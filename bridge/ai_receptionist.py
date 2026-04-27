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

def tts_speak(agi, text, tmp_dir='/tmp/voip_tts'):
    """Convert text to speech using piper and play it."""
    os.makedirs(tmp_dir, exist_ok=True)
    wav_file = os.path.join(tmp_dir, f'tts_{int(time.time()*1000)}.wav')
    asterisk_file = wav_file.replace('.wav', '')  # Asterisk needs no extension

    voice_model = '/share/voip/piper/en_US-lessac-medium.onnx'
    piper_bin = '/usr/local/bin/piper'

    if not os.path.exists(voice_model) or not os.path.exists(piper_bin):
        # Fallback to espeak
        agi.verbose(f'Piper not available, using espeak for: {text[:50]}')
        espeak_wav = wav_file.replace('.wav', '_espeak.wav')
        subprocess.run([
            'espeak', '-a', '150', '-s', '130', '-v', 'en', text,
            '--stdout'
        ], stdout=open(espeak_wav, 'wb'), stderr=subprocess.DEVNULL)
        # Convert to 8kHz mono
        subprocess.run([
            'sox', espeak_wav, '-r', '8000', '-c', '1', wav_file
        ], stderr=subprocess.DEVNULL)
        os.unlink(espeak_wav)
    else:
        # Use piper
        proc = subprocess.Popen(
            [piper_bin, '--model', voice_model, '--output_file', wav_file],
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )
        proc.communicate(input=text.encode())
        # Convert to 8kHz mono for Asterisk
        converted = wav_file.replace('.wav', '_8k.wav')
        subprocess.run([
            'sox', wav_file, '-r', '8000', '-c', '1', converted
        ], stderr=subprocess.DEVNULL)
        os.unlink(wav_file)
        os.rename(converted, wav_file)

    if os.path.exists(wav_file):
        agi.playback(asterisk_file)
        try:
            os.unlink(wav_file)
        except Exception:
            pass
    else:
        agi.verbose('TTS file generation failed')


# ---------------------------------------------------------------------------
# STT - Whisper
# ---------------------------------------------------------------------------

def stt_transcribe(wav_file):
    """Transcribe audio using Whisper."""
    try:
        import whisper
        model = whisper.load_model('tiny')
        result = model.transcribe(wav_file, language='en', fp16=False)
        return result.get('text', '').strip()
    except ImportError:
        # Fallback: try whisper CLI
        try:
            result = subprocess.run(
                ['whisper', wav_file, '--model', 'tiny', '--language', 'en',
                 '--output_format', 'txt', '--output_dir', '/tmp'],
                capture_output=True, text=True, timeout=30
            )
            txt_file = wav_file.replace('.wav', '.txt')
            if os.path.exists(txt_file):
                text = open(txt_file).read().strip()
                os.unlink(txt_file)
                return text
        except Exception as e:
            return ''
    except Exception as e:
        return ''


# ---------------------------------------------------------------------------
# Claude API
# ---------------------------------------------------------------------------

def claude_respond(messages, system_prompt, api_key, max_tokens=300):
    """Call Claude API and return response text."""
    try:
        resp = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
                'content-type': 'application/json',
            },
            json={
                'model': 'claude-sonnet-4-20250514',
                'max_tokens': max_tokens,
                'system': system_prompt,
                'messages': messages,
            },
            timeout=15,
        )
        data = resp.json()
        for block in data.get('content', []):
            if block.get('type') == 'text':
                return block['text'].strip()
    except Exception as e:
        return None
    return None


# ---------------------------------------------------------------------------
# HA notification
# ---------------------------------------------------------------------------

def ha_notify(ha_url, ha_token, title, message, critical=False):
    """Fire a HA notification."""
    try:
        payload = {
            'title': title,
            'message': message,
        }
        if critical:
            payload['data'] = {
                'push': {
                    'sound': {
                        'name': 'default',
                        'critical': 1,
                        'volume': 1.0,
                    }
                }
            }
        requests.post(
            f'{ha_url.rstrip("/")}/api/services/notify/notify',
            headers={
                'Authorization': f'Bearer {ha_token}',
                'Content-Type': 'application/json',
            },
            json=payload,
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

    # Config from environment
    ha_url      = os.environ.get('HA_URL', 'http://homeassistant:8123')
    ha_token    = os.environ.get('HA_TOKEN', '')
    api_key     = os.environ.get('ANTHROPIC_API_KEY', '')
    owner_name  = os.environ.get('OWNER_NAME', 'the owner')
    availability = os.environ.get('AVAILABILITY_INFO', 'often unavailable')

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
        'known_caller': bool(known_caller_name),
    }

    # Fire incoming event
    ha_event(ha_url, ha_token, 'voip_call_incoming', call_info)

    # Answer call
    agi.answer()
    time.sleep(0.5)

    # Build system prompt
    system_prompt = f"""You are an AI receptionist answering calls for {owner_name}.

{owner_name} is {availability}.

Your role:
1. Greet the caller warmly and professionally
2. Answer basic questions about {owner_name}'s availability
3. Take detailed messages when appropriate
4. Detect if the call is URGENT (emergency, time-sensitive, crisis)
5. Keep responses concise (1-3 sentences) since this is a phone call

Caller information:
- Phone number: {caller_id}
- Name: {display_name}
- Known contact: {'Yes' if known_caller_name else 'No'}

At the end of the conversation, provide a JSON summary in this exact format on the last line:
{{"summary": "Brief summary", "message": "Detailed message if any", "urgent": true/false}}

Keep your spoken responses natural and brief. The JSON summary only appears when you say goodbye."""

    conversation = []
    transcript = []
    max_turns = 8

    # Generate initial greeting
    greeting_prompt = f"Generate a brief, warm greeting for {'known contact ' + known_caller_name if known_caller_name else 'an unknown caller'}. Just the greeting, no JSON yet."
    greeting = claude_respond(
        [{'role': 'user', 'content': greeting_prompt}],
        system_prompt,
        api_key,
        max_tokens=100,
    )

    if not greeting:
        greeting = f"Hello, you've reached {owner_name}'s home. How can I help you today?"

    agi.verbose(f'Greeting: {greeting}')
    tts_speak(agi, greeting)
    transcript.append({'role': 'assistant', 'content': greeting})
    conversation.append({'role': 'assistant', 'content': greeting})

    # Fire answered event
    ha_event(ha_url, ha_token, 'voip_call_answered', call_info)

    # Conversation loop
    summary_data = {'summary': 'Call received', 'message': '', 'urgent': False}

    for turn in range(max_turns):
        # Record caller speech
        rec_file = f'/tmp/voip_rec_{unique_id}_{turn}'
        agi.verbose(f'Recording turn {turn}...')
        result = agi.record_file(rec_file, fmt='wav', timeout=8000, silence=2)

        wav_path = f'{rec_file}.wav'
        if not os.path.exists(wav_path):
            break

        # Check if file has meaningful audio (> 1KB)
        if os.path.getsize(wav_path) < 1024:
            os.unlink(wav_path)
            tts_speak(agi, "I'm sorry, I didn't catch that. Could you please repeat?")
            continue

        # Transcribe
        agi.verbose('Transcribing...')
        caller_text = stt_transcribe(wav_path)

        try:
            os.unlink(wav_path)
        except Exception:
            pass

        if not caller_text:
            tts_speak(agi, "I'm sorry, I had trouble hearing you. Could you repeat that?")
            continue

        agi.verbose(f'Caller said: {caller_text}')
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
                tts_speak(agi, spoken or "Thank you for calling. Goodbye!")
                transcript.append({'role': 'assistant', 'content': spoken})
            break

        # Get Claude response
        response = claude_respond(conversation, system_prompt, api_key, max_tokens=200)

        if not response:
            tts_speak(agi, "I'm sorry, I'm having some trouble. Please try calling back.")
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

        agi.verbose(f'Response: {spoken_response}')
        tts_speak(agi, spoken_response)
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
    notification_body = summary_text
    if message_text:
        notification_body += f'\n\nMessage: {message_text}'
    notification_body += f'\n\nTranscript: {transcript_file}'

    ha_notify(ha_url, ha_token, notification_title, notification_body, critical=urgent)

    # Fire ended event
    ha_event(ha_url, ha_token, 'voip_call_ended', {
        **call_info,
        'summary': summary_text,
        'urgent': urgent,
        'transcript_file': transcript_file,
    })

    agi.hangup()


if __name__ == '__main__':
    main()
