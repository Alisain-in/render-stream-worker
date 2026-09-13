FROM node:20-alpine

# Install FFmpeg, Python3, pip, curl (fast and lightweight Alpine packages)
RUN apk add --no-cache ffmpeg python3 py3-pip curl
RUN pip3 install --no-cache-dir --break-system-packages gdown

WORKDIR /usr/src/app

COPY package*.json ./
RUN npm install --production

COPY . .

EXPOSE 10000

CMD [ "node", "server.js" ]
