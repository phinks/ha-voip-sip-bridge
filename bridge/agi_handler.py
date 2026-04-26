#!/usr/bin/env python3
"""
agi_handler.py – Asterisk AGI script, invoked once per incoming call.

Responsibilities:
  1. Read call variables from Asterisk (caller ID, channel, unique ID).
  2. Notify Home Assistant of the incoming call via REST event.
  3. Optionally answer the call and play a TTS greeting.
  4. Wait briefly for DTMF input, map digits to HA events.
  5. Notify HA when the call ends.

Asterisk invokes this as:
    AGI(agi_handler.py,${CALLERID(num)},${CALLERID(name)},${EXTEN})
"""

import sys
import os
import time
import json
import requests
import subprocess
import threading

# ---------------------------------------------------------------------------
# Minimal AGI protocol implementation
# ---------------------------------------------------------------------------

class AGI:
    def __init__(self):
        self.env = {}
        self._parse_env()

    def _parse_env(self):
        """Read AGI environment variables from stdin."""
        while True:
            line = sys.stdin.readline()
            if not line or line.strip() == '':
                break
            if ':' in line:
                key, _, value = line.partition(':')
                self.env[key.strip()] = value.strip()

    def _send(self, cmd: str) -> str:
        sys.stdout.write(cmd + '\n')
        sys.stdout.flush()
        return sys.stdin.readline().strip()

    def _result_code(self, response: str) -> int:
        """Parse '200 result=N' → N, or -1 on error."""
        try:
            parts = response.split()
            for p in parts:
                if p.startswith('result='):
                    return int(p.split('=')[1].split('(')[0])
        except Exception:
            pass
        return -1

    def answer(self) -> int:
        return self._result_code(self._send('ANSWER'))

    def hangup(self) -> int:
        return self._result_code(self._send('HANGUP'))

    def playback(self, filename: str) -> int:
        """Play a sound file (no extension). Blocks until done."""
        resp = self._send(f'EXEC Playback {filename}')
        return self._result_code(resp)

    def wait_for_digit(self, timeout_ms: int = 5000) -> str:
        """Wait for a DTMF digit. Returns the digit char, or '' on timeout."""
        resp = self._send(f'WAIT FOR DIGIT {timeout_ms}')
        code = self._result_code(resp)
        if code > 0:
            return chr(code)
        return ''

    def get_variable(self, name: str) -> str:
        resp = self._send(f'GET VARIABLE {name}')
        if '(' in resp:
            return resp.split('(')[1].rstrip(')')
        return ''

    def set_variable(self, name: str, value: str):
        self._send(f'SET VARIABLE {name} "{value}"')

    def record(self, filename: str, silence_secs: int = 5, max_secs: int = 60):
        """Record audio to file (WAV)."""
        self._send(f'EXEC Record {filename}.wav|{silence_secs}|{max_secs}')

    def verbose(self, msg: str, level: int = 1):
        self._send(f'VERBOSE "{msg}" {level}')


# ---------------------------------------------------------------------------
# Home Assistant REST helper
# ---------------------------------------------------------------------------

def ha_post(path: str, data: dict, ha_url: str, ha_token: str, timeout: int = 5):
    """POST to HA REST API. Silently ignores errors so calls aren't disrupted."""
    try:
        resp = requests.post(
            f'{ha_url.rstrip("/")}{path}',
            headers={
                'Authorization': f'Bearer {ha_token}',
                'Content-Type': 'application/json',
            },
            json=data,
            timeout=timeout,
        )
        return resp.status_code
    except Exception as e:
        sys.stderr.write(f'[agi_handler] HA POST failed: {e}\n')
        return None


# ---------------------------------------------------------------------------
# Main call handler
# ---------------------------------------------------------------------------

def main():
    agi = AGI()

    # AGI environment
    caller_num  = agi.env.get('agi_callerid', 'unknown')
    caller_name = agi.env.get('agi_calleridname', '')
    channel     = agi.env.get('agi_channel', '')
    unique_id   = agi.env.get('agi_uniqueid', '')
    extension   = agi.env.get('agi_extension', '')

    # Command-line args: agi_handler.py <callerid_num> <callerid_name> <exten>
    # (these echo what's in agi_env but are passed explicitly for clarity)
    if len(sys.argv) >= 2:
        caller_num = sys.argv[1] or caller_num
    if len(sys.argv) >= 3:
        caller_name = sys.argv[2] or caller_name

    # Runtime config from environment (set by run.sh)
    ha_url       = os.environ.get('HA_URL', 'http://homeassistant:8123')
    ha_token     = os.environ.get('HA_TOKEN', '')
    auto_answer  = os.environ.get('AUTO_ANSWER', 'true').lower() == 'true'
    play_greeting= os.environ.get('PLAY_GREETING', 'true').lower() == 'true'
    record_calls = os.environ.get('RECORD_CALLS', 'false').lower() == 'true'

    call_info = {
        'caller_id':   caller_num,
        'caller_name': caller_name,
        'channel':     channel,
        'unique_id':   unique_id,
        'extension':   extension,
    }

    agi.verbose(f'Incoming call from {caller_num} ({caller_name}) uid={unique_id}')

    # ------------------------------------------------------------------
    # 1. Fire "incoming call" event to HA
    # ------------------------------------------------------------------
    ha_post('/api/events/voip_call_incoming', call_info, ha_url, ha_token)

    # ------------------------------------------------------------------
    # 2. Auto-answer and play greeting
    # ------------------------------------------------------------------
    if auto_answer:
        agi.answer()
        time.sleep(0.5)

        if play_greeting:
            greeting_wav = '/share/voip/greeting'
            if os.path.exists(f'{greeting_wav}.wav'):
                agi.playback(greeting_wav)
            else:
                # Fall back to built-in Asterisk sound files
                agi.playback('hello-world')

        # Fire "answered" event
        ha_post('/api/events/voip_call_answered', call_info, ha_url, ha_token)

        # ------------------------------------------------------------------
        # 3. Optionally record the call
        # ------------------------------------------------------------------
        if record_calls:
            rec_path = f'/var/spool/asterisk/recording/{unique_id}'
            agi.record(rec_path, silence_secs=5, max_secs=300)
            ha_post(
                '/api/events/voip_call_recorded',
                {**call_info, 'recording': f'{rec_path}.wav'},
                ha_url, ha_token,
            )

        # ------------------------------------------------------------------
        # 4. Simple DTMF IVR – wait for digit, fire event, repeat
        # ------------------------------------------------------------------
        agi.verbose('Waiting for DTMF...')
        while True:
            digit = agi.wait_for_digit(timeout_ms=30_000)
            if digit:
                ha_post(
                    '/api/events/voip_dtmf',
                    {**call_info, 'digit': digit},
                    ha_url, ha_token,
                )
            else:
                # 30-second silence timeout – end the call
                break

    # ------------------------------------------------------------------
    # 5. Fire "call ended" event and hang up
    # ------------------------------------------------------------------
    ha_post('/api/events/voip_call_ended', call_info, ha_url, ha_token)
    agi.hangup()


if __name__ == '__main__':
    main()
