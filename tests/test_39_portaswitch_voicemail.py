"""Voicemail save/patch support in PortaSwitchAdapter (WT-1878).

The PortaSwitch mailbox is an IMAP mailbox with no folders: the only per-message state
is its flags, and `set_mailbox_messages_flag` accepts just Seen, Answered and Flagged.
That forces two things this file pins down:

* `saved` is the \\Flagged flag - read out of the same `flags` list as `seen`, so a
  message the subscriber kept survives in PortaSwitch and is shared across their devices;
* a patch applies only the attributes that were sent. Each attribute is a separate
  PortaBilling call, so "not mentioned" and "false" must not collapse into one - saving
  a message must not mark it unseen on the way.

Trash and forwarding are deliberately absent here: the mailbox has neither, and both are
implemented and stored by Core alone.
"""
import os
import sys

import pytest

_app_path = os.path.join(os.path.dirname(__file__), '..', 'app')
sys.path.insert(0, _app_path)

# PortaSwitchSettings is instantiated at import time and its URL/credential fields are
# mandatory; supply throwaway values before importing the adapter.
os.environ.setdefault('PORTASWITCH_ADMIN_API_URL', 'https://pbx.example.com')
os.environ.setdefault('PORTASWITCH_ACCOUNT_API_URL', 'https://pbx.example.com')
os.environ.setdefault('PORTASWITCH_ADMIN_API_LOGIN', 'admin')
os.environ.setdefault('PORTASWITCH_ADMIN_API_TOKEN', 'token')
os.environ.setdefault('PORTASWITCH_SIP_SERVER_HOST', '1.2.3.4')

from app_config import AppConfig
from bss.adapters.portaswitch.adapter import PortaSwitchAdapter
from bss.adapters.portaswitch.serializer import Serializer
from bss.adapters.portaswitch.types import (
    PortaSwitchMailboxMessageFlag,
    PortaSwitchMailboxMessageFlagAction,
)
from bss.types import Capabilities, SessionInfo, UserVoicemailMessagePatch

MESSAGE_ID = "1654"
SESSION = SessionInfo(user_id="102398", access_token="tok")

SET = PortaSwitchMailboxMessageFlagAction.SET
UNSET = PortaSwitchMailboxMessageFlagAction.UNSET
SEEN = PortaSwitchMailboxMessageFlag.SEEN
FLAGGED = PortaSwitchMailboxMessageFlag.FLAGGED


def mailbox_message(flags=None, **overrides):
    """A message list row shaped the way PortaBilling returns it."""
    message = {
        "message_uid": 1654,
        "size": 5,
        "voicemail_duration": 3.45,
        "delivery_date": "07-Jun-2024 12:32:03 +0000",
    }
    if flags is not None:
        message["flags"] = flags
    message.update(overrides)
    return message


# --------------------------------------------------------------------------- #
# Serializer: saved comes from the mailbox flags
# --------------------------------------------------------------------------- #

class TestSavedFlag:
    def test_flagged_message_is_saved(self):
        assert Serializer.get_voicemail_message(mailbox_message(["\\Flagged"])).saved is True

    def test_unflagged_message_is_not_saved(self):
        assert Serializer.get_voicemail_message(mailbox_message(["\\Seen"])).saved is False

    def test_message_without_flags_key(self):
        # PortaBilling omits `flags` entirely on a message that carries none.
        message = Serializer.get_voicemail_message(mailbox_message())
        assert message.saved is False
        assert message.seen is False

    def test_seen_and_saved_are_independent(self):
        message = Serializer.get_voicemail_message(mailbox_message(["\\Seen", "\\Flagged"]))
        assert message.seen is True
        assert message.saved is True

    def test_answered_flag_does_not_mean_saved(self):
        # \Answered is a real IMAP flag the subscriber's mail client can set; it must
        # not be mistaken for our "saved" marker.
        assert Serializer.get_voicemail_message(mailbox_message(["\\Answered"])).saved is False

    def test_details_carry_saved_too(self):
        details = mailbox_message(
            ["\\Flagged"],
            **{
                "from": "Caller #123010 <123010@sip.webtrit.com>",
                "to": "123009 <123009@sip.webtrit.com>",
                "body_structures": [],
            },
        )
        assert Serializer.get_voicemail_message_details(details).saved is True


# --------------------------------------------------------------------------- #
# patch_voicemail_message: only what was sent is applied
# --------------------------------------------------------------------------- #

class FakeAccountAPI:
    def __init__(self):
        self.flag_calls = []

    async def set_mailbox_message_flag(self, access_token, message_id, flag, action):
        self.flag_calls.append((access_token, message_id, flag, action))
        return {}


def adapter():
    adapter = object.__new__(PortaSwitchAdapter)
    adapter._account_api = FakeAccountAPI()
    return adapter


async def patch(**attributes):
    """Patch a message with exactly the given attributes and report what was sent."""
    subject = adapter()
    result = await subject.patch_voicemail_message(
        SESSION, MESSAGE_ID, UserVoicemailMessagePatch(**attributes)
    )
    return subject._account_api.flag_calls, result


class TestPatchVoicemailMessage:
    @pytest.mark.asyncio
    async def test_marking_seen_sets_only_the_seen_flag(self):
        calls, result = await patch(seen=True)

        assert calls == [("tok", MESSAGE_ID, SEEN, SET)]
        assert result.seen is True

    @pytest.mark.asyncio
    async def test_marking_unseen_clears_the_flag(self):
        # False is a request to clear, not an absent attribute.
        calls, _ = await patch(seen=False)

        assert calls == [("tok", MESSAGE_ID, SEEN, UNSET)]

    @pytest.mark.asyncio
    async def test_saving_sets_only_the_flagged_flag(self):
        calls, result = await patch(saved=True)

        assert calls == [("tok", MESSAGE_ID, FLAGGED, SET)]
        assert result.saved is True

    @pytest.mark.asyncio
    async def test_unsaving_clears_the_flagged_flag(self):
        calls, _ = await patch(saved=False)

        assert calls == [("tok", MESSAGE_ID, FLAGGED, UNSET)]

    @pytest.mark.asyncio
    async def test_saving_does_not_touch_seen(self):
        # The regression this guards: reading `body.seen` unconditionally turned every
        # save into "save and mark unseen", because an omitted seen arrives as None.
        calls, result = await patch(saved=True)

        assert [flag for _, _, flag, _ in calls] == [FLAGGED]
        assert 'seen' not in result.model_dump(exclude_none=True)

    @pytest.mark.asyncio
    async def test_both_attributes_are_applied(self):
        calls, result = await patch(seen=True, saved=True)

        assert calls == [
            ("tok", MESSAGE_ID, SEEN, SET),
            ("tok", MESSAGE_ID, FLAGGED, SET),
        ]
        assert result.seen is True and result.saved is True

    @pytest.mark.asyncio
    async def test_explicit_null_is_not_a_change(self):
        # A client that serialises unset optionals as null must not have its flags
        # cleared. Presence alone is not enough: pydantic counts an explicit null as
        # set, so reading it as falsy would send remove_flag.
        calls, result = await patch(seen=None, saved=None)

        assert calls == []
        assert result.model_dump(exclude_none=True) == {}

    @pytest.mark.asyncio
    async def test_null_alongside_a_real_change_is_ignored(self):
        calls, result = await patch(seen=True, saved=None)

        assert calls == [("tok", MESSAGE_ID, SEEN, SET)]
        assert result.model_dump(exclude_none=True) == {"seen": True}

    @pytest.mark.asyncio
    async def test_empty_patch_changes_nothing(self):
        calls, result = await patch()

        assert calls == []
        assert result.model_dump(exclude_none=True) == {}

    @pytest.mark.asyncio
    async def test_response_echoes_only_the_requested_attributes(self):
        _, result = await patch(saved=False)

        assert result.model_dump(exclude_none=True) == {"saved": False}


# --------------------------------------------------------------------------- #
# Capabilities
# --------------------------------------------------------------------------- #

VOICEMAIL_SUB_CAPABILITIES = {
    Capabilities.voicemail_save,
    Capabilities.voicemail_trash,
    Capabilities.voicemail_forward,
}


def capabilities_of(**config):
    """Capabilities the real PortaSwitch adapter reports under this config.

    Goes through the shipped class rather than a stub, so the calls share the one
    CAPABILITIES list they would share in a running process - which is what makes the
    "does a disabled capability stick" question answerable at all.
    """
    subject = object.__new__(PortaSwitchAdapter)
    subject.config = AppConfig({"Capabilities": config})
    return set(subject.calculate_capabilities())


class TestVoicemailCapabilities:
    def test_portaswitch_codes_for_the_whole_set(self):
        # save is backed by \Flagged; trash and forward live in Core and are advertised
        # so a client can tell a Core that speaks them from one that does not.
        coded = set(PortaSwitchAdapter.CAPABILITIES)

        assert VOICEMAIL_SUB_CAPABILITIES <= coded
        # The precondition for the dependency filter: without this the three would be
        # stripped from the real adapter every time.
        assert Capabilities.voicemail in coded

    def test_enabled_by_default_alongside_voicemail(self):
        assert VOICEMAIL_SUB_CAPABILITIES <= capabilities_of(VOICEMAIL="1")

    def test_save_and_trash_have_no_switch_of_their_own(self):
        # Both come with the voicemail screen; the config variables that used to turn
        # them off are gone, so a leftover value in a deployment config is inert.
        enabled = capabilities_of(VOICEMAIL="1", VOICEMAIL_SAVE="0", VOICEMAIL_TRASH="0")

        assert Capabilities.voicemail_save in enabled
        assert Capabilities.voicemail_trash in enabled

    def test_dropped_when_voicemail_itself_is_off(self):
        # Advertising a voicemail control on a deployment with no voicemail screen would
        # describe something the client can never reach.
        enabled = capabilities_of(VOICEMAIL="0")

        assert Capabilities.voicemail not in enabled
        assert not (VOICEMAIL_SUB_CAPABILITIES & enabled)

    def test_forward_is_the_only_one_still_switchable(self):
        enabled = capabilities_of(VOICEMAIL="1", VOICEMAIL_FORWARD="0")

        assert Capabilities.voicemail_forward not in enabled
        assert Capabilities.voicemail_save in enabled
        assert Capabilities.voicemail_trash in enabled


class TestCapabilityCalculation:
    """`calculate_capabilities` used to edit the class-level CAPABILITIES list it was
    given, and PortaSwitchAdapter calls it twice - in __init__ and in initialize()."""

    def test_the_class_list_survives(self):
        coded = list(PortaSwitchAdapter.CAPABILITIES)

        capabilities_of(VOICEMAIL="0")
        capabilities_of(VOICEMAIL="1")

        assert list(PortaSwitchAdapter.CAPABILITIES) == coded

    def test_disabling_does_not_stick_across_calls(self):
        # The consequence of the leak: `voicemail` removed from the shared list by the
        # first call could never be switched back on by a later one.
        assert Capabilities.voicemail not in capabilities_of(VOICEMAIL="0")
        assert Capabilities.voicemail in capabilities_of(VOICEMAIL="1")

    def test_repeated_calls_agree(self):
        assert capabilities_of(VOICEMAIL="1") == capabilities_of(VOICEMAIL="1")
