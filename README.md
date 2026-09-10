# An adapter between WebTrit and external VoIP system or PBX
## Overview
This is an application that serves as a mapper of API requests
from WebTrit cloud back-end to a 3rd-party system (e.g. hosted PBX)
to retrieve the data about users, so they can use WebTrit's
mobile or web dialer.

The idea is to expand it with additional modules for conecting to
specific types of systems.

More details about how (and why) WebTrit connects to external VoIP
or BSS systems in this [blog article](https://webtrit.com/insights/webrtc-softphone-third-party-voip-switches-cloud-pbx-systems/)

## What's included
* general FastAPI application that processes API requests from WebTrit
* bss/connectors/example.py module which mimics the functionality of
connecting to a real system. User info is stored in the source code and 
info about other extensions or previously made calls is generated randomly.
* set of tests (in tests/ folder) which you can use to test your own 
adapter once it is ready
* Dockerfile for packaging

## Usage
### With an "example" module
* cd app
* docker build -t xyz .
* make your container running on a public IP address (I assume 1.2.3.4)
* Verify that things are working on by
pytest --server http://1.2.3.4 tests
* Apply http://1.2.3.4 in the configuration of your WebTrit instance, so it
sends requests to your API

### Creating your own adapter
* Create your own module xyz in bss/adapters/ folder (use example.py as a template) and define a class (inherited from BSSAdapter) called XYZAdapter
* set BSS_ADAPTER_MODULE environment variable to bss.connectors.xyz
* set BSS_ADAPTER_CLASS environment variable to the name XYZAdapter
* set additional variables as needed (e.g. path to the REST API of your VoIP system)
* start the app ```
cd app
uvicorn main:app --port 8000
```
* test it: ```
pip install pytest-lazy-fixture
pytest --server http://<your-server-ip-and-port> --user user1 --password xyz tests
```

## Capability switches

`GET /system-info` reports a `supported` list, and WebTrit Core passes it to the client
apps so they can show or hide a control. Two things decide what is in it: the
`CAPABILITIES` list on the adapter class (what the adapter has code for) and a
per-deployment env variable for each entry (whether this installation offers it).

The env variable name is `CAPABILITIES_<OPTION>` — prefixed with `<APP_NAME>_` when an
`APP_NAME` is set — where the options and their defaults are
`CONFIG_CAPABILITIES_OPTIONS` in `app/bss/adapters/__init__.py`. Values are read as
booleans (`1`/`true`/`yes`/`y`). A capability the adapter class does not list cannot be
switched on.

Voicemail (WT-1878):

| Variable | Default | Purpose |
|---|---|---|
| `CAPABILITIES_VOICEMAIL` | `false` | The voicemail screen at all. Every voicemail functionality below is dropped when this is off |
| `CAPABILITIES_VOICEMAIL_FORWARD` | `true` | Passing a message on to another user. Implemented and stored by Core — the mailbox has no forward API |

`voicemailSave` and `voicemailTrash` have no switch of their own: whenever the
voicemail screen is on, both are advertised. Save is backed by the IMAP `\Flagged`
flag on the PortaSwitch mailbox; trash means a `DELETE` moves the message to a trash
it can be restored from, and is Core's own.

`voicemailTrash` and `voicemailForward` describe Core behaviour, not PortaSwitch
behaviour; they are advertised here only so a client can tell a Core that speaks them
from one that does not.
