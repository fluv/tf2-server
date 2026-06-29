FROM node:lts

WORKDIR /app
COPY yarn.lock package.json ./
RUN yarn install --ignore-scripts && yarn cache clean
COPY . .
RUN yarn build:css
CMD ["yarn", "start"]
