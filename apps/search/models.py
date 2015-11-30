import time
import datetime
import pymongo
import pyes
import redis
import celery
import mongoengine as mongo
from pyes.query import MatchQuery
from django.conf import settings
from django.contrib.auth.models import User
from apps.search.tasks import IndexSubscriptionsForSearch
from apps.search.tasks import IndexSubscriptionsChunkForSearch
from apps.search.tasks import IndexFeedsForSearch
from utils import log as logging
from utils.feed_functions import chunks

class MUserSearch(mongo.Document):
    '''Search index state of a user's subscriptions.'''
    user_id                  = mongo.IntField(unique=True)
    last_search_date         = mongo.DateTimeField()
    subscriptions_indexed    = mongo.BooleanField()
    subscriptions_indexing   = mongo.BooleanField()
    
    meta = {
        'collection': 'user_search',
        'indexes': ['user_id'],
        'index_drop_dups': True,
        'allow_inheritance': False,
    }
    
    @classmethod
    def get_user(cls, user_id, create=True):
        try:
            user_search = cls.objects.read_preference(pymongo.ReadPreference.PRIMARY)\
                                     .get(user_id=user_id)
        except cls.DoesNotExist:
            if create:
                user_search = cls.objects.create(user_id=user_id)
            else:
                user_search = None
        
        return user_search
    
    def touch_search_date(self):
        if not self.subscriptions_indexed and not self.subscriptions_indexing:
            self.schedule_index_subscriptions_for_search()
            self.subscriptions_indexing = True

        self.last_search_date = datetime.datetime.now()
        self.save()

    def schedule_index_subscriptions_for_search(self):
        IndexSubscriptionsForSearch.apply_async(kwargs=dict(user_id=self.user_id), 
                                                queue='search_indexer_tasker')
        
    # Should be run as a background task
    def index_subscriptions_for_search(self):
        from apps.rss_feeds.models import Feed
        from apps.reader.models import UserSubscription
        
        SearchStory.create_elasticsearch_mapping()
        
        start = time.time()
        user = User.objects.get(pk=self.user_id)
        r = redis.Redis(connection_pool=settings.REDIS_PUBSUB_POOL)
        r.publish(user.username, 'search_index_complete:start')
        
        subscriptions = UserSubscription.objects.filter(user=user).only('feed')
        total = subscriptions.count()
        
        feed_ids = []
        for sub in subscriptions:
            try:
                feed_ids.append(sub.feed.pk)
            except Feed.DoesNotExist:
                continue
        
        feed_id_chunks = [c for c in chunks(feed_ids, 6)]
        logging.user(user, "~FCIndexing ~SB%s feeds~SN in %s chunks..." % 
                     (total, len(feed_id_chunks)))
        
        tasks = [IndexSubscriptionsChunkForSearch().s(feed_ids=feed_id_chunk,
                                                      user_id=self.user_id
                                                      ).set(queue='search_indexer')
                 for feed_id_chunk in feed_id_chunks]
        group = celery.group(*tasks)
        res = group.apply_async(queue='search_indexer')
        res.join_native()
        
        duration = time.time() - start
        logging.user(user, "~FCIndexed ~SB%s feeds~SN in ~FM~SB%s~FC~SN sec." % 
                     (total, round(duration, 2)))
        r.publish(user.username, 'search_index_complete:done')
        
        self.subscriptions_indexed = True
        self.subscriptions_indexing = False
        self.save()
    
    def index_subscriptions_chunk_for_search(self, feed_ids):
        from apps.rss_feeds.models import Feed
        r = redis.Redis(connection_pool=settings.REDIS_PUBSUB_POOL)
        user = User.objects.get(pk=self.user_id)

        logging.user(user, "~FCIndexing %s feeds..." % len(feed_ids))

        for feed_id in feed_ids:
            feed = Feed.get_by_id(feed_id)
            if not feed: continue
            
            feed.index_stories_for_search()
            
        r.publish(user.username, 'search_index_complete:feeds:%s' % 
                  ','.join([str(f) for f in feed_ids]))
    
    @classmethod
    def schedule_index_feeds_for_search(cls, feed_ids, user_id):
        user_search = cls.get_user(user_id, create=False)
        if (not user_search or 
            not user_search.subscriptions_indexed or 
            user_search.subscriptions_indexing):
            # User hasn't searched before.
            return
        
        if not isinstance(feed_ids, list):
            feed_ids = [feed_ids]
        IndexFeedsForSearch.apply_async(kwargs=dict(feed_ids=feed_ids, user_id=user_id), 
                                        queue='search_indexer')
    
    @classmethod
    def index_feeds_for_search(cls, feed_ids, user_id):
        from apps.rss_feeds.models import Feed
        user = User.objects.get(pk=user_id)

        logging.user(user, "~SB~FCIndexing %s~FC by request..." % feed_ids)

        for feed_id in feed_ids:
            feed = Feed.get_by_id(feed_id)
            if not feed: continue
            
            feed.index_stories_for_search()
        
    @classmethod
    def remove_all(cls, drop_index=False):
        user_searches = cls.objects.all()
        logging.info(" ---> ~SN~FRRemoving ~SB%s~SN user searches..." % user_searches.count())
        for user_search in user_searches:
            try:
                user_search.remove()
            except Exception, e:
                print " ****> Error on search removal: %s" % e
        
        # You only need to drop the index if there is data you want to clear.
        # A new search server won't need this, as there isn't anything to drop.
        if drop_index:
            logging.info(" ---> ~FRRemoving stories search index...")
            SearchStory.drop()
        
    def remove(self):
        from apps.rss_feeds.models import Feed
        from apps.reader.models import UserSubscription

        user = User.objects.get(pk=self.user_id)
        subscriptions = UserSubscription.objects.filter(user=self.user_id, 
                                                        feed__search_indexed=True)
        total = subscriptions.count()
        removed = 0
        
        for sub in subscriptions:
            try:
                feed = sub.feed
            except Feed.DoesNotExist:
                continue
            feed.search_indexed = False
            feed.save()
            removed += 1
            
        logging.user(user, "~FCRemoved ~SB%s/%s feed's search indexes~SN for ~SB~FB%s~FC~SN." % 
                     (removed, total, user.username))
        self.delete()

class SearchStory:
    
    ES = pyes.ES(settings.ELASTICSEARCH_STORY_HOSTS)
    name = "stories"
    
    @classmethod
    def index_name(cls):
        return "%s-index" % cls.name
        
    @classmethod
    def type_name(cls):
        return "%s-type" % cls.name
        
    @classmethod
    def create_elasticsearch_mapping(cls, delete=False):
        if delete:
            cls.ES.indices.delete_index_if_exists("%s-index" % cls.name)
        cls.ES.indices.create_index_if_missing("%s-index" % cls.name)
        mapping = { 
            'title': {
                'boost': 3.0,
                'index': 'analyzed',
                'store': 'no',
                'type': 'string',
                'analyzer': 'snowball',
            },
            'content': {
                'boost': 1.0,
                'index': 'analyzed',
                'store': 'no',
                'type': 'string',
                'analyzer': 'snowball',
            },
            'tags': {
                'boost': 2.0,
                'index': 'analyzed',
                'store': 'no',
                'type': 'string',
                'analyzer': 'snowball',
            },
            'author': {
                'boost': 1.0,
                'index': 'analyzed',
                'store': 'no',
                'type': 'string',   
                'analyzer': 'keyword',
            },
            'feed_id': {
                'store': 'no',
                'type': 'integer'
            },
            'date': {
                'store': 'no',
                'type': 'date',
            }
        }
        cls.ES.indices.put_mapping("%s-type" % cls.name, {
            'properties': mapping,
            '_source': {'enabled': False},
        }, ["%s-index" % cls.name])
        
    @classmethod
    def index(cls, story_hash, story_title, story_content, story_tags, story_author, story_feed_id, 
              story_date):
        doc = {
            "content"   : story_content,
            "title"     : story_title,
            "tags"      : ', '.join(story_tags),
            "author"    : story_author,
            "feed_id"   : story_feed_id,
            "date"      : story_date,
        }
        try:
            cls.ES.index(doc, "%s-index" % cls.name, "%s-type" % cls.name, story_hash)
        except pyes.exceptions.NoServerAvailable:
            logging.debug(" ***> ~FRNo search server available.")
    
    @classmethod
    def remove(cls, story_hash):
        try:
            cls.ES.delete("%s-index" % cls.name, "%s-type" % cls.name, story_hash)
        except pyes.exceptions.NoServerAvailable:
            logging.debug(" ***> ~FRNo search server available.")
        
    @classmethod
    def drop(cls):
        cls.ES.indices.delete_index_if_exists("%s-index" % cls.name)
        
    @classmethod
    def query(cls, feed_ids, query, order, offset, limit):
        cls.create_elasticsearch_mapping()
        cls.ES.indices.refresh()
        
        sort     = "date:desc" if order == "newest" else "date:asc"
        string_q = pyes.query.StringQuery(query, default_operator="AND")
        feed_q   = pyes.query.TermsQuery('feed_id', feed_ids[:1000])
        q        = pyes.query.BoolQuery(must=[string_q, feed_q])
        try:
            results  = cls.ES.search(q, indices=cls.index_name(), doc_types=[cls.type_name()],
                                     partial_fields={}, sort=sort, start=offset, size=limit)
        except pyes.exceptions.NoServerAvailable:
            logging.debug(" ***> ~FRNo search server available.")
            return []

        logging.info(" ---> ~FG~SNSearch ~FCstories~FG for: ~SB%s~SN (across %s feed%s)" % 
                     (query, len(feed_ids), 's' if len(feed_ids) != 1 else ''))
        
        return [r.get_id() for r in results]


class SearchFeed:
    
    ES = pyes.ES(settings.ELASTICSEARCH_FEED_HOSTS)
    name = "feeds"
    
    @classmethod
    def index_name(cls):
        return "%s-index" % cls.name
        
    @classmethod
    def type_name(cls):
        return "%s-type" % cls.name
        
    @classmethod
    def create_elasticsearch_mapping(cls, delete=False):
        if delete:
            cls.ES.indices.delete_index_if_exists("%s-index" % cls.name)
        settings =  {
            "index" : {
                "analysis": {
                    "analyzer": {
                        "edgengram_analyzer": {
                            "filter": ["edgengram"],
                            "tokenizer": "lowercase",
                            "type": "custom"
                        },
                        "ngram_analyzer": {
                            "filter": ["ngram"],
                            "tokenizer": "lowercase",
                            "type": "custom"
                        }
                    },
                    "filter": {
                        "edgengram": {
                            "max_gram": "15",
                            "min_gram": "2",
                            "type": "edgeNGram"
                        },
                        "ngram": {
                            "max_gram": "15",
                            "min_gram": "3",
                            "type": "nGram"
                        }
                    },
                    "tokenizer": {
                        "edgengram_tokenizer": {
                            "max_gram": "15",
                            "min_gram": "2",
                            "side": "front",
                            "type": "edgeNGram"
                        },
                        "ngram_tokenizer": {
                            "max_gram": "15",
                            "min_gram": "3",
                            "type": "nGram"
                        }
                    }
                }
            }
        }
        cls.ES.indices.create_index_if_missing("%s-index" % cls.name, settings)

        mapping = {
            "address": {
                "analyzer": "edgengram_analyzer",
                "store": True,
                "term_vector": "with_positions_offsets",
                "type": "string"
            },
            "feed_id": {
                "store": True,
                "type": "string"
            },
            "num_subscribers": {
                "index": "analyzed",
                "store": True,
                "type": "long"
            },
            "title": {
                "analyzer": "edgengram_analyzer",
                "store": True,
                "term_vector": "with_positions_offsets",
                "type": "string"
            }
        }
        cls.ES.indices.put_mapping("%s-type" % cls.name, {
            'properties': mapping,
        }, ["%s-index" % cls.name])
        
    @classmethod
    def index(cls, feed_id, title, address, link, num_subscribers):
        doc = {
            "feed_id"           : feed_id,
            "title"             : title,
            "address"           : address,
            "link"              : link,
            "num_subscribers"   : num_subscribers,
        }
        try:
            cls.ES.index(doc, "%s-index" % cls.name, "%s-type" % cls.name, feed_id)
        except pyes.exceptions.NoServerAvailable:
            logging.debug(" ***> ~FRNo search server available.")
        
    @classmethod
    def query(cls, text):
        cls.create_elasticsearch_mapping()
        try:
            cls.ES.default_indices = cls.index_name()
            cls.ES.indices.refresh()
        except pyes.exceptions.NoServerAvailable:
            logging.debug(" ***> ~FRNo search server available.")
            return []
        
        logging.info("~FGSearch ~FCfeeds~FG by address: ~SB%s" % text)
        q = MatchQuery('address', text, operator="and", type="phrase")
        results = cls.ES.search(query=q, sort="num_subscribers:desc", size=5,
                                doc_types=[cls.type_name()])

        if not results.total:
            logging.info("~FGSearch ~FCfeeds~FG by title: ~SB%s" % text)
            q = MatchQuery('title', text, operator="and")
            results = cls.ES.search(query=q, sort="num_subscribers:desc", size=5,
                                    doc_types=[cls.type_name()])
            
        if not results.total:
            logging.info("~FGSearch ~FCfeeds~FG by link: ~SB%s" % text)
            q = MatchQuery('link', text, operator="and")
            results = cls.ES.search(query=q, sort="num_subscribers:desc", size=5,
                                    doc_types=[cls.type_name()])
            
        return results
    
    @classmethod
    def export_csv(cls):
        import djqscsv

        qs = Feed.objects.filter(num_subscribers__gte=20).values('id', 'feed_title', 'feed_address', 'feed_link', 'num_subscribers')
        csv = djqscsv.render_to_csv_response(qs).content
        f = open('feeds.csv', 'w+')
        f.write(csv)
        f.close()
        