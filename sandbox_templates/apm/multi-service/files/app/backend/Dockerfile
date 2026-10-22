FROM node:20-slim
WORKDIR /app
COPY package.json .
RUN npm install --omit=dev
COPY index.js .
# NODE_OPTIONS (set in inventory.env) is how the tracer gets loaded before the app.
CMD ["node", "index.js"]
