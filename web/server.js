const express = require('express')
const app = module.exports = express()
const nunjucks = require('nunjucks')

const pino = require('pino')()
const pinoHttp = require('pino-http')( {logger: pino} )
app.use(pinoHttp)

nunjucks.configure([
    'node_modules/govuk-frontend/dist',
    'views'
], {
    autoescape: true,
    express: app
})

app.use('/govuk',express.static('node_modules/govuk-frontend/dist/govuk'))
app.use('/assets',express.static('assets'))

const port = process.env.PORT || 3000
app.listen(port, () => pino.info(`Listening on ${port}`))

require('./routes')
