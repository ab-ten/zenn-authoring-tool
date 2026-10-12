FROM node:24.21.0-bookworm-slim

ENV NODE_ENV=production \
    TZ=Asia/Tokyo

RUN npm install --global --no-audit --no-fund zenn-cli@0.5.6 \
    && zenn --version \
    && mkdir -p /preview /preview/books \
    && chown -R node:node /preview

WORKDIR /preview
USER node

EXPOSE 8000

CMD ["zenn", "preview", "--host", "0.0.0.0", "--no-watch"]

COPY --chown=node:node generate-links.mjs entrypoint.sh /opt/previewer/
COPY --chown=node:node tests/ /opt/previewer/tests/

ENTRYPOINT ["/bin/sh", "/opt/previewer/entrypoint.sh"]
