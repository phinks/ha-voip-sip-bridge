#!/usr/bin/env python3
import os
"""
command_api.py - REST API for HA to control active calls via Asterisk AMI.
Compatible with Python 3.12+. Uses raw TCP AMI sockets.
"""
import argparse
import logging
import sys
import socket
import threading
import time
from flask import Flask, request, jsonify

logging.basicConfig(
    level=logging.INFO,
    format='[command_api] %(levelname)s %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

app = Flask(__name__)
_channels = {}
_ami_secret = ""
_ami_lock = threading.Lock()
_ami_sock = None


def ami_send(action: dict) -> str:
    global _ami_sock
    if _ami_sock is None:
        return 'Error: AMI not connected'
    msg = ''.join(f'{k}: {v}\r\n' for k, v in action.items()) + '\r\n'
    try:
        with _ami_lock:
            _ami_sock.sendall(msg.encode())
        return 'OK'
    except Exception as e:
        return f'Error: {e}'


@app.route('/status', methods=['GET'])
def status():
    return jsonify({
        'active_channels': _channels,
        'ami_connected':   _ami_sock is not None,
    })


@app.route('/call/<unique_id>/hangup', methods=['POST'])
def hangup_call(unique_id):
    channel = _channels.get(unique_id)
    if not channel:
        return jsonify({'error': f'No channel for {unique_id}'}), 404
    result = ami_send({'Action': 'Hangup', 'Channel': channel})
    return jsonify({'result': result})


@app.route('/call/hangup_all', methods=['POST'])
def hangup_all():
    results = {}
    for uid, channel in list(_channels.items()):
        results[uid] = ami_send({'Action': 'Hangup', 'Channel': channel})
    return jsonify({'results': results})


@app.route('/call/<unique_id>/play', methods=['POST'])
def play_audio(unique_id):
    body = request.get_json(silent=True) or {}
    audio_file = body.get('file', '')
    if not audio_file:
        return jsonify({'error': 'Missing "file"'}), 400
    channel = _channels.get(unique_id)
    if not channel:
        return jsonify({'error': f'No channel for {unique_id}'}), 404
    result = ami_send({
        'Action':      'Originate',
        'Channel':     channel,
        'Application': 'Playback',
        'Data':        audio_file,
        'Async':       'true',
    })
    return jsonify({'result': result})


def ami_listener(host, port, secret):
    global _ami_sock
    while True:
        try:
            log.info(f'Connecting to AMI {host}:{port}...')
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((host, port))
            _ami_sock = sock
            sock.recv(1024)  # banner
            sock.sendall(f'Action: Login\r\nUsername: bridge\r\nSecret: {secret}\r\n\r\n'.encode())
            log.info('AMI connected.')

            buf = ''
            while True:
                data = sock.recv(4096).decode(errors='replace')
                if not data:
                    break
                buf += data
                while '\r\n\r\n' in buf:
                    block, buf = buf.split('\r\n\r\n', 1)
                    lines = block.strip().split('\r\n')
                    event = {}
                    for line in lines:
                        if ':' in line:
                            k, _, v = line.partition(':')
                            event[k.strip()] = v.strip()
                    name = event.get('Event', '')
                    uid  = event.get('Uniqueid', '')
                    if name == 'Newchannel' and uid:
                        _channels[uid] = event.get('Channel', '')
                    elif name == 'Hangup' and uid in _channels:
                        del _channels[uid]
        except Exception as e:
            log.error(f'AMI listener error: {e}')
        finally:
            _ami_sock = None
        log.warning('AMI disconnected - retrying in 10s...')
        time.sleep(10)



@app.route("/cmd", methods=["GET"])
def run_cmd():
    import socket as _s, time as _t
    cmd = request.args.get("c", "core show version")
    s = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    try:
        s.connect(("127.0.0.1", 5038))
        s.recv(1024)
        s.sendall(("Action: Login\r\nUsername: bridge\r\nSecret: " + os.environ.get("AMI_SECRET", "") + "\r\n\r\n").encode())
        _t.sleep(0.3)
        s.recv(4096)
        s.sendall(f"Action: Command\r\nCommand: {cmd}\r\n\r\n".encode())
        _t.sleep(1)
        s.settimeout(2)
        result = b""
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk: break
                result += chunk
        except: pass
        return result.decode(errors="replace"), 200, {"Content-Type": "text/plain"}
    finally:
        s.close()

@app.route("/log", methods=["GET"])
def get_log():
    path = request.args.get("f", "/tmp/debug.log")
    try:
        return open(path).read(), 200, {"Content-Type": "text/plain"}
    except Exception as e:
        return str(e), 404


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ami-host',   default='127.0.0.1')
    parser.add_argument('--ami-port',   type=int, default=5038)
    parser.add_argument('--ami-secret', required=True)
    parser.add_argument('--api-port',   type=int, default=8089)
    args = parser.parse_args()
    global _ami_secret
    _ami_secret = args.ami_secret

    t = threading.Thread(
        target=ami_listener,
        args=(args.ami_host, args.ami_port, args.ami_secret),
        daemon=True,
    )
    t.start()
    time.sleep(3)

    log.info(f'Starting command API on 0.0.0.0:{args.api_port}')
    app.run(host='0.0.0.0', port=args.api_port, threaded=True)


if __name__ == '__main__':
    main()

