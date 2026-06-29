FROM node:lts

WORKDIR /app
COPY yarn.lock package.json ./
RUN yarn install && yarn cache clean && yarn build:css
COPY . .
CMD ["yarn", "start"]
