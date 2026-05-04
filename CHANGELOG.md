## 1.0.24
- Added `people` config — each household member has name, title, HA person entity, calendar, extension, and mobile
- AI receptionist queries live presence (home/away) and calendar events at call time
- Availability and transfer logic now driven by people config; legacy fields still work as fallback

## 1.0.23
- Added call recording support (MixMonitor) when `record_calls` is enabled
- Added web dashboard with clickable call transcripts
- Dashboard displays times in configured timezone
- Audio playback on transcript pages when recording is available
- Added sidebar panel (ingress) for direct HA access

## 1.0.22
- Added transferable people — AI can transfer calls to named extensions or phone numbers
- Added per-DID labels and auto-capture of unseen DIDs
- Added known contacts support
- Added timezone configuration
- Added TTS voice selection
- Added custom tenets (rules) for the AI receptionist
- Fixed push notifications not firing when caller hangs up mid-response (SIGPIPE handling)

## 1.0.19
- Added AI receptionist powered by Claude and Groq Whisper STT
- AI handles inbound calls, takes messages, and sends HA push notifications
- Urgent call detection triggers critical push notifications
- Robocall engagement mode

## 1.0.0
- Initial release
- SIP registration via PJSIP
- Fires HA events on inbound calls (voip_channel_created, voip_channel_hangup)
- AMI monitor for real-time call events
- Command API for HA → addon control
- NAT traversal support
