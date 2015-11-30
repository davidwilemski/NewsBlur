express = require 'express'
mongo = require 'mongodb'

DEV = process.env.NODE_ENV == 'development'
MONGODB_SERVER = if DEV then 'localhost' else 'db_mongo'
MONGODB_PORT = parseInt(process.env.MONGODB_PORT or 27017, 10)

if DEV
    server = new mongo.Server(MONGODB_SERVER, MONGODB_PORT, 
        auto_reconnect: true
        poolSize: 12)
else
    server = new mongo.ReplSetServers(
        [new mongo.Server( MONGODB_SERVER, MONGODB_PORT, { auto_reconnect: true } )]
        {rs_name: 'nbset'})

db = new mongo.Db('newsblur', server,
    readPreference: mongo.ReadPreference.SECONDARY_PREFERRED
    safe: false)

app = express.createServer()
app.use express.bodyParser()
    
db.open (err, client) =>
    client.collection "feed_icons", (err, @collection) =>
    
app.get /^\/rss_feeds\/icon\/(\d+)\/?/, (req, res) =>
    feed_id = parseInt(req.params, 10)
    etag = req.header('If-None-Match')
    @collection.findOne feed_id: feed_id, (err, docs) ->
        console.log "Req: #{feed_id}, etag: #{etag}/#{docs?.color} (err: #{err}, docs? #{!!(docs and docs.data)})"
        if not err and etag and docs and docs?.color == etag
            res.send 304
        else if not err and docs and docs.data
                res.header 'etag', docs.color
                res.send new Buffer(docs.data, 'base64'), 
                    "Content-Type": "image/png"
        else
            if DEV
                res.redirect '/media/img/icons/circular/world.png' 
            else
                res.redirect 'https://www.newsblur.com/media/img/icons/circular/world.png' 

app.listen 3030
