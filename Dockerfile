FROM python:3.12-slim

# Node runs PayPal's official MCP server, which answers the owner's questions about their account.
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @paypal/mcp@1.8.1

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV HOST=0.0.0.0
CMD ["python3", "server.py"]
