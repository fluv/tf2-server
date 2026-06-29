const app = require('..')

app.use(require('./health.js'))

app.get('/', (req, res) => res.render('index.html',
    {
        hostname: process.env.TF2_HOST || req.headers.host,
        port: process.env.TF2_PORT || 27015
    })
)
