#!/usr/bin/env python3
"""
ai_receptionist.py - AI-powered receptionist AGI script.

Handles inbound calls with:
- Personalized greeting (known vs unknown callers)
- Full conversation with Claude AI
- Whisper STT for caller speech recognition
- Piper TTS for AI responses
- Urgency detection → HA critical notification
- Full transcript saved to /share/voip/transcripts/
"""

import sys
import os
import json
import time
import wave
import struct
import subprocess
import tempfile
import requests
import datetime
from zoneinfo import ZoneInfo

def call_log(unique_id, role, text):
    """Append to conversation log file."""
    try:
        with open(f"/share/voip/conversation_{unique_id}.log", "a") as f:
            f.write(f"[{datetime.datetime.now().strftime("%H:%M:%S")}] {role}: {text}\n")
    except Exception:
        pass
import re

# ---------------------------------------------------------------------------
# Minimal AGI protocol
# ---------------------------------------------------------------------------

class AGI:
    def __init__(self):
        self.env = {}
        self._parse_env()

    def _parse_env(self):
        while True:
            line = sys.stdin.readline()
            if not line or line.strip() == '':
                break
            if ':' in line:
                key, _, value = line.partition(':')
                self.env[key.strip()] = value.strip()

    def _send(self, cmd):
        sys.stdout.write(cmd + '\n')
        sys.stdout.flush()
        return sys.stdin.readline().strip()

    def _result(self, resp):
        try:
            for p in resp.split():
                if p.startswith('result='):
                    return int(p.split('=')[1].split('(')[0])
        except Exception:
            pass
        return -1

    def answer(self):
        return self._result(self._send('ANSWER'))

    def hangup(self):
        return self._result(self._send('HANGUP'))

    def playback(self, filename):
        """Play a sound file (no extension). Blocks until complete."""
        return self._result(self._send(f'EXEC Playback {filename}'))

    def stream_file(self, filename, escape_digits=''):
        """Stream a file, allowing DTMF escape."""
        resp = self._send(f'STREAM FILE {filename} "{escape_digits}"')
        return self._result(resp)

    def record_file(self, filename, fmt='wav', escape_digits='#', timeout=10000, silence=3):
        """Record audio to file. Returns (digit, endpos)."""
        resp = self._send(
            f'RECORD FILE {filename} {fmt} "{escape_digits}" {timeout} s={silence}'
        )
        return self._result(resp)

    def get_variable(self, name):
        resp = self._send(f'GET VARIABLE {name}')
        if '(' in resp:
            return resp.split('(')[1].rstrip(')')
        return ''

    def verbose(self, msg, level=1):
        self._send(f'VERBOSE "{msg}" {level}')

    def set_variable(self, name, value):
        self._send(f'SET VARIABLE {name} "{value}"')


# ---------------------------------------------------------------------------
# TTS - Piper
# ---------------------------------------------------------------------------

def claude_respond(messages, system_prompt, api_key, max_tokens=300):
    """Call Claude API and return response text."""
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-5",
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": messages,
            },
            timeout=15,
        )
        open("/tmp/debug.log", "a").write(f"Claude status: {resp.status_code}, body: {resp.text[:300]}\n")
        data = resp.json()
        for block in data.get("content", []):
            if block.get("type") == "text":
                return block["text"].strip()
    except Exception as e:
        open("/tmp/debug.log", "a").write(f"Claude exception: {e}\n")
        return None
    return None


def tts_speak(agi, text, ha_url, ha_token, voice="en-US-JennyNeural", tmp_dir="/tmp/voip_tts"):
    """Convert text to speech using HA Cloud TTS."""
    import os
    os.makedirs(tmp_dir, exist_ok=True)
    wav_file = os.path.join(tmp_dir, f"tts_{int(time.time()*1000)}.wav")
    asterisk_file = wav_file.replace(".wav", "")
    try:
        resp = requests.post(
            f"{ha_url.rstrip("/")}/api/tts_get_url",
            headers={
                "Authorization": f"Bearer {ha_token}",
                "Content-Type": "application/json",
            },
            json={
                "platform": "cloud",
                "message": text,
                "language": "en-US",
                "options": {"voice": voice},
            },
            timeout=10,
        )
        if resp.status_code == 200:
            audio_url = resp.json().get("url", "")
            if audio_url:
                # Download the audio
                audio_resp = requests.get(audio_url, timeout=10)
                mp3_file = wav_file.replace(".wav", ".mp3")
                with open(mp3_file, "wb") as f:
                    f.write(audio_resp.content)
                # Convert to 8kHz mono WAV for Asterisk
                subprocess.run(["ffmpeg", "-i", mp3_file, "-ar", "8000", "-ac", "1", wav_file, "-y"], stderr=subprocess.DEVNULL)
                os.unlink(mp3_file)
    except Exception as e:
        agi.verbose(f"HA TTS failed: {e}")
    # Fallback to espeak
    if not os.path.exists(wav_file):
        espeak_wav = wav_file.replace(".wav", "_espeak.wav")
        subprocess.run([
            "espeak", "-a", "150", "-s", "130", "-v", "en", text,
            "--stdout"
        ], stdout=open(espeak_wav, "wb"), stderr=subprocess.DEVNULL)
        subprocess.run([
            "sox", espeak_wav, "-r", "8000", "-c", "1", wav_file
        ], stderr=subprocess.DEVNULL)
        try: os.unlink(espeak_wav)
        except: pass
    if os.path.exists(wav_file):
        agi.playback(asterisk_file)
        try: os.unlink(wav_file)
        except: pass


def stt_transcribe(wav_file, groq_api_key):
    """Transcribe audio using Groq Whisper API."""
    try:
        with open(wav_file, "rb") as f:
            resp = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {groq_api_key}"},
                files={"file": ("audio.wav", f, "audio/wav")},
                data={"model": "whisper-large-v3-turbo", "language": "en"},
                timeout=15,
            )
        if resp.status_code == 200:
            return resp.json().get("text", "").strip()
        open("/tmp/debug.log", "a").write(f"Groq status: {resp.status_code}, body: {resp.text[:200]}\n")
        return ""
    except Exception as e:
        return ""
    except Exception as e:
        open("/tmp/debug.log", "a").write(f"Groq exception: {e}\n")
        return ""

def ha_persistent_notification(ha_url, ha_token, title, message, notification_id):
    """Create a persistent notification in HA (visible in the notifications panel)."""
    try:
        requests.post(
            f'{ha_url.rstrip("/")}/api/services/persistent_notification/create',
            headers={
                'Authorization': f'Bearer {ha_token}',
                'Content-Type': 'application/json',
            },
            json={
                'title': title,
                'message': message,
                'notification_id': notification_id,
            },
            timeout=5,
        )
    except Exception:
        pass


def ha_notify(ha_url, ha_token, title, message, critical=False):
    """Fire a HA mobile notification."""
    try:
        data = {'url': '/'}
        if critical:
            data['push'] = {
                'sound': {
                    'name': 'default',
                    'critical': 1,
                    'volume': 1.0,
                }
            }
        requests.post(
            f'{ha_url.rstrip("/")}/api/services/notify/notify',
            headers={
                'Authorization': f'Bearer {ha_token}',
                'Content-Type': 'application/json',
            },
            json={
                'title': title,
                'message': message,
                'data': data,
            },
            timeout=5,
        )
    except Exception:
        pass


def ha_event(ha_url, ha_token, event_name, data):
    """Fire a HA event."""
    try:
        requests.post(
            f'{ha_url.rstrip("/")}/api/events/{event_name}',
            headers={
                'Authorization': f'Bearer {ha_token}',
                'Content-Type': 'application/json',
            },
            json=data,
            timeout=5,
        )
    except Exception:
        pass


def auto_capture_did(extension):
    """Record an unseen DID to /share/voip/known_dids.json. Returns True if it was new."""
    if not extension:
        return False
    did_file = '/share/voip/known_dids.json'
    try:
        os.makedirs('/share/voip', exist_ok=True)
        known = {}
        if os.path.exists(did_file):
            with open(did_file) as f:
                known = json.load(f)
        if extension in known:
            return False
        known[extension] = {'first_seen': datetime.datetime.now().isoformat()}
        with open(did_file, 'w') as f:
            json.dump(known, f, indent=2)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Save transcript
# ---------------------------------------------------------------------------

def save_transcript(caller_id, caller_name, transcript, summary, urgent, unique_id):
    """Save call transcript to /share/voip/transcripts/."""
    os.makedirs('/share/voip/transcripts', exist_ok=True)
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f'/share/voip/transcripts/{ts}_{caller_id}.txt'
    with open(filename, 'w') as f:
        f.write(f'Date: {datetime.datetime.now().isoformat()}\n')
        f.write(f'Caller ID: {caller_id}\n')
        f.write(f'Caller Name: {caller_name}\n')
        f.write(f'Unique ID: {unique_id}\n')
        f.write(f'Urgent: {urgent}\n')
        f.write(f'\nSummary:\n{summary}\n')
        f.write(f'\nTranscript:\n')
        for turn in transcript:
            f.write(f'{turn["role"].upper()}: {turn["content"]}\n')
    return filename


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    agi = AGI()

    # AGI environment
    caller_id   = agi.env.get('agi_callerid', 'unknown')
    caller_name = agi.env.get('agi_calleridname', '')
    channel     = agi.env.get('agi_channel', '')
    unique_id   = agi.env.get('agi_uniqueid', '')
    extension   = agi.env.get('agi_extension', '')

    if len(sys.argv) >= 2:
        caller_id = sys.argv[1] or caller_id
    if len(sys.argv) >= 3:
        caller_name = sys.argv[2] or caller_name
    if len(sys.argv) >= 5:
        extension = sys.argv[4] or extension

    # Config from environment
    ha_url      = os.environ.get('HA_URL', 'http://homeassistant:8123')
    ha_token    = os.environ.get('HA_TOKEN', '')
    api_key     = os.environ.get("ANTHROPIC_API_KEY", "")
    groq_key    = os.environ.get("GROQ_API_KEY", "")
    owner_name  = os.environ.get('OWNER_NAME', 'the owner')
    availability = os.environ.get('AVAILABILITY_INFO', 'often unavailable')
    tts_voice   = os.environ.get('TTS_VOICE', 'en-US-JennyNeural')
    tz          = ZoneInfo(os.environ.get('TIMEZONE', 'UTC') or 'UTC')

    tenets = []
    try:
        tenets = json.loads(os.environ.get('TENETS', '[]'))
    except Exception:
        pass

    did_numbers = {}
    try:
        for entry in json.loads(os.environ.get('DID_NUMBERS', '[]')):
            num = entry.get('number', '').strip().lstrip('+')
            if num:
                did_numbers[num] = entry.get('description', '').strip()
    except Exception:
        pass

    # Parse transferable people: [{"name": "Paul", "extension": "PJSIP/paul", "phone": "SIP/voip/+1..."}]
    transferable = {}  # lowercase name -> {"name": str, "dial": str}
    try:
        people_raw = json.loads(os.environ.get('TRANSFERABLE_PEOPLE', '[]'))
        for p in people_raw:
            name = p.get('name', '').strip()
            if not name:
                continue
            parts = [p.get('extension', '').strip(), p.get('phone', '').strip()]
            dial = '&'.join(x for x in parts if x)
            transferable[name.lower()] = {'name': name, 'dial': dial}
    except Exception:
        pass

    # Auto-capture unseen DIDs
    if extension and extension not in did_numbers:
        if auto_capture_did(extension):
            ha_persistent_notification(
                ha_url, ha_token,
                f'New DID seen: {extension}',
                (
                    f'A call was received on **{extension}**, which has no label yet.\n\n'
                    f'To label it, add it to the **did_numbers** list in the VoIP SIP Bridge configuration:\n\n'
                    f'```\nnumber: "{extension}"\ndescription: "My description"\n```'
                ),
                notification_id=f'voip_new_did_{extension}',
            )

    # Load known contacts
    known_contacts = {}
    contacts_file = '/share/voip/known_contacts.json'
    if os.path.exists(contacts_file):
        try:
            known_contacts = json.load(open(contacts_file))
        except Exception:
            pass

    # Identify caller
    known_caller_name = known_contacts.get(caller_id, {}).get('name', '')
    display_name = known_caller_name or caller_name or 'there'

    call_info = {
        'caller_id':   caller_id,
        'caller_name': caller_name or known_caller_name,
        'channel':     channel,
        'unique_id':   unique_id,
        'extension':   extension,
        'did':         extension,
        'known_caller': bool(known_caller_name),
    }

    # Fire incoming event
    ha_event(ha_url, ha_token, 'voip_call_incoming', call_info)
    # Play immediate greeting while Claude prepares
    if os.path.exists("/share/voip/greeting"):
        agi.playback("/share/voip/greeting")
    # Build system prompt
    transfer_section = ""
    if transferable:
        names = ', '.join(p['name'] for p in transferable.values())
        transfer_section = f"""
People who can receive transferred calls: {names}
- If the caller asks to speak with one of these people, confirm the name and offer to transfer them.
- If the caller asks for someone NOT on the list, politely explain that person is not available and offer to take a message.
- When transferring, set "transfer_to" in the JSON summary to the exact name from the list above."""

    now = datetime.datetime.now(tz=tz).strftime('%A, %d %B %Y at %H:%M %Z')

    tenets_section = ""
    if tenets:
        rules = '\n'.join(f'{i+1}. {t}' for i, t in enumerate(tenets))
        tenets_section = f"\nRules you must always follow:\n{rules}\n"

    did_desc = did_numbers.get(extension.lstrip('+'), '') if extension else ''
    did_line = f"This call was received on number: {extension}{f' ({did_desc})' if did_desc else ''}.\n" if extension else ""

    caller_line = (
        f"The caller's phone number is recognized as {known_caller_name}. "
        f"You know who this is, but follow your rules about when to use their name.\n"
        if known_caller_name else
        "The caller is not in the contact list.\n"
    )

    system_prompt = f"""You are an AI receptionist answering calls for {owner_name}.
The current date and time is {now}.
{did_line}{caller_line}
{owner_name} is {availability}.
{tenets_section}
Your role:
1. Greet the caller warmly and professionally
2. Ask who they would like to speak with or how you can help
3. Answer basic questions about {owner_name}'s availability
4. Take detailed messages when appropriate
5. Detect if the call is URGENT (emergency, time-sensitive, crisis)
6. Keep responses concise (1-3 sentences) since this is a phone call
{transfer_section}
At the end of the conversation, provide a JSON summary in this exact format on the last line:
{{"summary": "Brief summary", "message": "Detailed message if any", "urgent": false, "transfer_to": ""}}

Set "transfer_to" to the person's name if transferring, otherwise leave it as an empty string.
Keep your spoken responses natural and brief. The JSON summary only appears when you say goodbye."""

    conversation = []
    transcript = []
    max_turns = 8

    # Generate initial greeting
    greeting_prompt = "Generate a brief, warm professional greeting. Do not address the caller by name. Ask how you can help or who they would like to speak with. Just the greeting, no JSON yet."
    greeting = claude_respond(
        [{'role': 'user', 'content': greeting_prompt}],
        system_prompt,
        api_key,
        max_tokens=100,
    )

    if not greeting:
        greeting = f"Hello, you've reached {owner_name}'s home. How can I help you today?"

    agi.verbose(f'Greeting: {greeting}')
    tts_speak(agi, greeting, ha_url, ha_token, tts_voice)
    transcript.append({'role': 'assistant', 'content': greeting})
    conversation.append({'role': 'assistant', 'content': greeting})

    # Fire answered event
    ha_event(ha_url, ha_token, 'voip_call_answered', call_info)

    # Conversation loop
    summary_data = {'summary': 'Call received', 'message': '', 'urgent': False}

    for turn in range(max_turns):
        # Record caller speech
        rec_file = f"/var/spool/asterisk/recording/voip_rec_{unique_id}_{turn}"
        agi.verbose(f'Recording turn {turn}...')
        result = agi.record_file(rec_file, fmt="wav", timeout=8000, silence=2)
        open("/tmp/debug.log", "a").write(f"Turn {turn}: result={result}, file={rec_file}.wav, exists={os.path.exists(rec_file + ".wav")}\n")
        agi.verbose(f"Record result: {result}, checking: {rec_file}.wav")

        wav_path = f'{rec_file}.wav'
        if not os.path.exists(wav_path):
            break

        # Check if file has meaningful audio (> 1KB)
        if os.path.getsize(wav_path) < 1024:
            os.unlink(wav_path)
            tts_speak(agi, "I'm sorry, I didn't catch that. Could you please repeat?", ha_url, ha_token, tts_voice)
            continue

        # Convert to 16kHz for better STT accuracy
        wav_16k = wav_path.replace(".wav", "_16k.wav")
        subprocess.run(["ffmpeg", "-i", wav_path, "-ar", "16000", "-ac", "1", wav_16k, "-y"], stderr=subprocess.DEVNULL)
        if os.path.exists(wav_16k):
            os.unlink(wav_path)
            wav_path = wav_16k
        # Transcribe
        agi.verbose('Transcribing...')
        caller_text = stt_transcribe(wav_path, groq_key)
        open("/tmp/debug.log", "a").write(f"STT result: {repr(caller_text)}\n")

        try:
            os.unlink(wav_path)
        except Exception:
            pass

        if not caller_text:
            tts_speak(agi, "I'm sorry, I had trouble hearing you. Could you repeat that?", ha_url, ha_token, tts_voice)
            continue

        agi.verbose(f"Caller said: {caller_text}")
        call_log(unique_id, "CALLER", caller_text)
        transcript.append({'role': 'user', 'content': caller_text})
        conversation.append({'role': 'user', 'content': caller_text})

        # Check for goodbye
        goodbye_words = ['goodbye', 'bye', 'thanks bye', 'thank you goodbye', 'that\'s all']
        if any(w in caller_text.lower() for w in goodbye_words):
            # Get final summary from Claude
            conversation.append({
                'role': 'user',
                'content': 'Please say goodbye and provide the JSON summary on the last line.'
            })
            final = claude_respond(conversation, system_prompt, api_key, max_tokens=200)
            if final:
                # Extract JSON from last line
                lines = final.strip().split('\n')
                spoken = final
                for line in reversed(lines):
                    try:
                        summary_data = json.loads(line.strip())
                        spoken = '\n'.join(lines[:lines.index(line)]).strip()
                        break
                    except Exception:
                        continue
                tts_speak(agi, spoken or "Thank you for calling. Goodbye!", ha_url, ha_token, tts_voice)
                transcript.append({'role': 'assistant', 'content': spoken})
            break

        # Get Claude response
        response = claude_respond(conversation, system_prompt, api_key, max_tokens=200)
        open("/tmp/debug.log", "a").write(f"Claude response: {repr(response)}\n")

        if not response:
            tts_speak(agi, "I'm sorry, I'm having some trouble. Please try calling back.", ha_url, ha_token, tts_voice)
            break

        # Check if response contains JSON summary (end of conversation)
        lines = response.strip().split('\n')
        spoken_response = response
        for line in reversed(lines):
            try:
                summary_data = json.loads(line.strip())
                spoken_response = '\n'.join(lines[:lines.index(line)]).strip()
                break
            except Exception:
                continue

        agi.verbose(f"Response: {spoken_response}")
        call_log(unique_id, "AI", spoken_response)
        tts_speak(agi, spoken_response, ha_url, ha_token, tts_voice)
        transcript.append({'role': 'assistant', 'content': spoken_response})
        conversation.append({'role': 'assistant', 'content': response})

        # If Claude included summary, end conversation
        if summary_data.get('summary') != 'Call received':
            break

    # Save transcript
    transcript_file = save_transcript(
        caller_id, caller_name or known_caller_name,
        transcript, summary_data.get('summary', ''),
        summary_data.get('urgent', False), unique_id
    )

    # Send HA notification
    urgent = summary_data.get('urgent', False)
    message_text = summary_data.get('message', '')
    summary_text = summary_data.get('summary', 'Call received')

    notification_title = f'{"🚨 URGENT " if urgent else "📞 "}Call from {display_name} ({caller_id})'

    # Push notification body — summary + message so it's readable without opening HA
    push_body = summary_text
    if message_text:
        push_body += f'\n\nMessage: {message_text}'

    # Persistent notification body — full transcript
    persistent_body = push_body
    for turn in transcript:
        persistent_body += f'\n\n**{turn["role"].upper()}:** {turn["content"]}'

    ha_persistent_notification(
        ha_url, ha_token,
        notification_title,
        persistent_body,
        notification_id=f'voip_call_{unique_id}',
    )
    ha_notify(ha_url, ha_token, notification_title, push_body, critical=urgent)

    # Execute transfer if requested
    transfer_name = summary_data.get('transfer_to', '').strip().lower()
    transfer_target = transferable.get(transfer_name)
    if transfer_target and transfer_target['dial']:
        ha_event(ha_url, ha_token, 'voip_call_transferred', {
            **call_info,
            'transfer_to': transfer_target['name'],
        })
        agi._send(f"EXEC Dial {transfer_target['dial']}")
    else:
        # Fire ended event only when not transferring (Dial handles its own hangup)
        ha_event(ha_url, ha_token, 'voip_call_ended', {
            **call_info,
            'summary': summary_text,
            'urgent': urgent,
            'transcript_file': transcript_file,
        })
        agi.hangup()


if __name__ == '__main__':
    main()
