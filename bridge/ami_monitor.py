#!/usr/bin/env python3
"""
ami_monitor.py - Connects to Asterisk AMI via raw TCP and fires HA events.
Compatible with Python 3.12+.
"""
import asyncio
import argparse
import logging
import sys
import aiohttp

logging.basicConfig(
    level=logging.INFO,
    format='[ami_monitor] %(levelname)s %(message)s',
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


async def ha_event(session, ha_url, ha_token, event_name, data):
    url = f'{ha_url.rstrip("/")}/api/events/{event_name}'
    headers = {
        'Authorization': f'Bearer {ha_token}',
        'Content-Type': 'application/json',
    }
    try:
        async with session.post(url, headers=headers, json=data,
                                timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status not in (200, 201):
                log.warning(f'HA returned {resp.status} for {event_name}')
    except Exception as e:
        log.warning(f'HA event failed {event_name}: {e}')


def parse_block(lines):
    result = {}
    for line in lines:
        if ':' in line:
            key, _, value = line.partition(':')
            result[key.strip()] = value.strip()
    return result


async def handle_event(event, session, ha_url, ha_token):
    name = event.get('Event', '')

    async def send(ha_name, data):
        await ha_event(session, ha_url, ha_token, ha_name, data)

    if name == 'Newchannel':
        await send('voip_channel_created', {
            'channel':     event.get('Channel', ''),
            'caller_id':   event.get('CallerIDNum', ''),
            'caller_name': event.get('CallerIDName', ''),
            'unique_id':   event.get('Uniqueid', ''),
        })
    elif name == 'Hangup':
        await send('voip_channel_hangup', {
            'channel':    event.get('Channel', ''),
            'caller_id':  event.get('CallerIDNum', ''),
            'unique_id':  event.get('Uniqueid', ''),
            'cause':      event.get('Cause', ''),
            'cause_text': event.get('Cause-txt', ''),
        })
    elif name in ('PeerStatus', 'Registry'):
        await send('voip_registration_status', {
            'status': event.get('PeerStatus', event.get('Status', '')),
            'peer':   event.get('Peer', event.get('Domain', '')),
        })
    elif name == 'Hold':
        await send('voip_call_hold', {
            'channel':   event.get('Channel', ''),
            'unique_id': event.get('Uniqueid', ''),
            'on_hold':   True,
        })
    elif name == 'Unhold':
        await send('voip_call_hold', {
            'channel':   event.get('Channel', ''),
            'unique_id': event.get('Uniqueid', ''),
            'on_hold':   False,
        })


async def run(args):
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                log.info(f'Connecting to AMI {args.ami_host}:{args.ami_port}...')
                reader, writer = await asyncio.open_connection(args.ami_host, args.ami_port)
                await reader.readline()  # banner
                login = (
                    f'Action: Login\r\nUsername: bridge\r\nSecret: {args.ami_secret}\r\n\r\n'
                )
                writer.write(login.encode())
                await writer.drain()
                log.info('AMI connected.')

                buf = ''
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break
                    buf += data.decode(errors='replace')
                    while '\r\n\r\n' in buf:
                        block, buf = buf.split('\r\n\r\n', 1)
                        lines = block.strip().split('\r\n')
                        event = parse_block(lines)
                        await handle_event(event, session, args.ha_url, args.ha_token)

            except Exception as e:
                log.error(f'AMI error: {e}')

            log.warning('AMI disconnected - retrying in 10s...')
            await asyncio.sleep(10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ami-host',   default='127.0.0.1')
    parser.add_argument('--ami-port',   type=int, default=5038)
    parser.add_argument('--ami-secret', required=True)
    parser.add_argument('--ha-url',     required=True)
    parser.add_argument('--ha-token',   required=True)
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
