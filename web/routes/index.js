const app = require('..')

app.use(require('./health.js'))

app.get('/', (req, res) => res.render('index.html'))
