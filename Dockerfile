FROM alpine:3.21

RUN apk add --no-cache \
    pjproject \
    asterisk \
    asterisk-opus \
    asterisk-srtp \
    asterisk-sounds-en \
    asterisk-curl \
    ffmpeg \
    espeak \
    python3 \
    py3-pip \
    gettext \
    sox \
    && rm -rf /var/cache/apk/*

COPY bridge/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

COPY bridge/ /opt/bridge/
RUN chmod +x /opt/bridge/*.py

RUN mkdir -p /var/lib/asterisk/agi-bin \
    && mkdir -p /var/spool/asterisk/recording \
    && mkdir -p /share/voip \
    && mkdir -p /etc/asterisk \
    && mkdir -p /tmp/voip

# Copy static Asterisk configs (pjsip uses #include from /tmp/voip/)
COPY asterisk/asterisk.conf  /etc/asterisk/asterisk.conf
COPY asterisk/logger.conf    /etc/asterisk/logger.conf
COPY asterisk/modules.conf   /etc/asterisk/modules.conf
COPY asterisk/pjsip.conf     /etc/asterisk/pjsip.conf
COPY asterisk/extensions.conf /etc/asterisk/extensions.conf
COPY asterisk/manager.conf   /etc/asterisk/manager.conf

# Pre-create empty dynamic config files so Asterisk starts cleanly
RUN touch /tmp/voip/pjsip_dynamic.conf \
    && touch /tmp/voip/extensions_dynamic.conf \
    && touch /tmp/voip/manager_dynamic.conf

# Install AGI handler
COPY bridge/agi_handler.py /var/lib/asterisk/agi-bin/agi_handler.py
COPY bridge/ai_receptionist.py /var/lib/asterisk/agi-bin/ai_receptionist.py
RUN chmod +x /var/lib/asterisk/agi-bin/agi_handler.py /var/lib/asterisk/agi-bin/ai_receptionist.py

COPY run.sh /run.sh
RUN chmod +x /run.sh

ENTRYPOINT ["/bin/sh", "/run.sh"]
