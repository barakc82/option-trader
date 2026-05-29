FROM gnzsnz/ib-gateway:latest

ENV APP_DIR=/home/option-trader

# Install Python and ib_insync
USER root
RUN apt-get update && apt-get install -y \
    telnet \
    less \
    vim \
    cron \
    python3 \
    python3-pip \
    nginx

RUN pip3 install ib_insync colorlog pytz exchange_calendars gspread psutil twilio dash aiohttp nest_asyncio Brotli --break-system-packages \
    && rm -rf /var/lib/apt/lists/*
#USER ibgateway  # Switch back to non-root for security


RUN mkdir -p ${APP_DIR}
COPY app ${APP_DIR}/app
COPY utilities ${APP_DIR}/utilities
COPY frontend/dist/option-trader-dashboard ${APP_DIR}/frontend
COPY logs ${APP_DIR}/logs
COPY cache ${APP_DIR}/cache
COPY config ${APP_DIR}/config
COPY resources ${APP_DIR}/resources
RUN mkdir -p ${APP_DIR}/shared

# ── Nginx config ──────────────────────────────────────────────
COPY docker/nginx.conf /etc/nginx/sites-available/default
# ──────────────────────────────────────────────────────────────

WORKDIR ${APP_DIR}

COPY docker/*.sh .

# Make the wrapper script executable
RUN chmod +x *.sh
RUN (crontab -l 2>/dev/null; echo "0 * * * * ${APP_DIR}/trim_log.sh") | crontab -

WORKDIR ${APP_DIR}/logs

RUN echo "alias showlog='less \$(ls -1 option_trader_*.log | sort | tail -1)'" > ~/.bashrc
RUN echo "alias taillog='tail -f \$(ls -1 option_trader_*.log | sort | tail -1)'" >> ~/.bashrc
RUN echo "alias showsupervisor='less supervisor.log'" >> ~/.bashrc
RUN echo "alias tailsupervisor='tail -f supervisor.log'" >> ~/.bashrc
RUN echo "alias runsupervisor='python3 -m app.options_trader_supervisor'" >> ~/.bashrc
RUN echo "alias restartsupervisor='pkill -f app.options_trader_supervisor; cd /home/option-trader && python3 -m app.options_trader_supervisor &'" >> ~/.bashrc
RUN echo "alias restarttrader='echo \"{\\\"should_restart_option_trader\\\": 1}\" > /home/option-trader/config/supervisor_config.json'" >> ~/.bashrc

EXPOSE 8080

# Set the wrapper script as the command to run when the container starts
CMD ["../start.sh"]
