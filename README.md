# VoIP SIP Bridge for Home Assistant

Registers a VoIP SIP extension with Home Assistant. Fires events on incoming calls and optionally runs an AI-powered receptionist.

## Features

- Registers with any SIP provider (CallCentric, VoIP.ms, etc.)
- Fires HA events on incoming calls (`voip_call_incoming`, `voip_call_answered`, `voip_call_ended`)
- Optional AI receptionist powered by Claude (Anthropic)
- Whisper speech-to-text for caller transcription
- Piper TTS for natural voice responses
- Urgency detection with critical iOS notifications
- Full call transcripts saved to `/share/voip/transcripts/`

## Installation

1. In HA go to **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Add: `https://github.com/phinks/ha-voip-sip-bridge`
3. Find **VoIP SIP Bridge** and install

## Configuration

| Option | Description |
|---|---|
| `voip_username` | Your SIP provider username |
| `voip_password` | Your SIP provider password |
| `voip_domain` | SIP server domain or IP |
| `ha_token` | HA long-lived access token |
| `ai_receptionist` | Enable AI receptionist |
| `anthropic_api_key` | Required if AI receptionist enabled |
| `owner_name` | Your name for the receptionist to use |
| `availability_info` | Context about your availability |

## Known Contacts

Edit `/share/voip/known_contacts.json` to add known callers:

```json
{
  "12125551234": {"name": "John Smith", "notes": "Close friend"},
  "14155559876": {"name": "Jane Doe", "notes": "Family"}
}
```

## HA Events

| Event | Description |
|---|---|
| `voip_call_incoming` | Call arriving |
| `voip_call_answered` | Call answered |
| `voip_call_ended` | Call ended (includes summary if AI enabled) |
| `voip_registration_status` | SIP registration change |
