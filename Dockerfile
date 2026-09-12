FROM node:18-bullseye-slim

# Install FFmpeg and Python (for gdown)
RUN apt-get update && apt-get install -y ffmpeg python3-pip curl && rm -rf /var/lib/apt/lists/*
RUN pip3 install gdown --break-system-packages || pip3 install gdown

# Create app directory
WORKDIR /usr/src/app

# Install app dependencies
COPY package*.json ./
RUN npm install

# Bundle app source
COPY . .

EXPOSE 10000

CMD [ "node", "server.js" ]
