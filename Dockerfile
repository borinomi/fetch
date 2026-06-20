FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g single-file-cli \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN node --input-type=module -e "\
import { writeFileSync } from 'fs'; \
import { script, hookScript, zipScript } from '/usr/lib/node_modules/single-file-cli/lib/single-file-bundle.js'; \
writeFileSync('/app/singlefile-injected.js', script); \
writeFileSync('/app/singlefile-hook.js', hookScript); \
writeFileSync('/app/singlefile-zip.js', zipScript); \
"

EXPOSE 8010

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8010"]