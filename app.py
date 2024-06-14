#!/usr/bin/env python
"""mercury radio"""
import datetime
import logging
import os
import asyncio
import pickle
from random import choice
import tekore as tk
from dotenv import load_dotenv
from quart import Quart, request, redirect, render_template, session
from peewee import Model, TextField, BlobField, DateTimeField
from peewee import SQL, CharField, ForeignKeyField, IntegerField
from peewee import TimestampField, AutoField, IntegrityError, fn
from playhouse.db_url import connect

# pylint: disable=W0718,global-statement
# pylint: disable=broad-exception-caught

load_dotenv()  # take environment variables from .env

db = connect(os.environ['DATABASE_URL'], autorollback=True)

app = Quart(__name__)
app.secret_key = os.getenv("APP_SECRET", default="1234567890")
conf = tk.config_from_environment()
cred = tk.Credentials(*conf)
token_spotify = tk.request_client_token(*conf[:2])
spotify = tk.Spotify(token_spotify, asynchronous=True)
tasks = set()
auths = {}

logging.basicConfig(
    level=logging.INFO,
    # format='%(asctime)s %(name)s %(module)s %(funcName)s %(levelname)s %(message)s',
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt="%Y-%m-%d %H:%M:%S"
    )

httpx_logger = logging.getLogger('httpx')
httpx_logger.setLevel(os.getenv("LOGLEVEL_HTTPX", default="INFO"))

# initial app running message
hypercorn_error = logging.getLogger("hypercorn.error")
hypercorn_error.disabled = True

# access log
hypercorn_access = logging.getLogger("hypercorn.access")
hypercorn_access.disabled = True

quart_logger = logging.getLogger('quart.app')
# quart_logger.basicConfig(
#     level=logging.INFO,
#     format='%(asctime)s %(name)s %(module)s %(funcName)s %(levelname)s %(message)s',
#     # format='%(asctime)s %(levelname)s %(message)s',
#     datefmt="%Y-%m-%d %H:%M:%S"
#     )

class BaseModel(Model):
    """A base model that will use our database"""
    class Meta:
        """meta"""
        database = db


class User(BaseModel):
    """track users"""
    id = TextField(primary_key=True)
    token = BlobField()
    last_active = DateTimeField(constraints=[SQL('DEFAULT CURRENT_TIMESTAMP')])


class Track(BaseModel):
    """track tracks"""
    trackid = CharField(primary_key=True)
    trackname = TextField()


class Rating(BaseModel):
    """track likes"""
    user_id = ForeignKeyField(User, backref="ratings")
    # trackid = ForeignKeyField(Track, backref="trackid")
    trackid = CharField()
    trackname = TextField()
    rating = IntegerField()
    last_played = DateTimeField(constraints=[SQL('DEFAULT CURRENT_TIMESTAMP')])

    class Meta:
        """meta"""
        indexes = (
            (('user_id', 'trackid'), True),
        )


class PlayHistory(BaseModel):
    """track the history of songs played"""
    trackid = CharField()
    played_at = TimestampField()

    class Meta:
        """meta"""
        indexes = (
            (('trackid', 'played_at'), True),
        )


class UpcomingQueue(BaseModel):
    """track the upcoming songs"""
    id = AutoField
    trackid = ForeignKeyField(Track, backref="trackid")
    queued_at = DateTimeField(constraints=[SQL('DEFAULT CURRENT_TIMESTAMP')])


@app.before_request
def before_request():
    """save cookies even if you close your browser"""
    session.permanent = True

    # make sure database is open
    if db.is_closed():
        logging.warning("web_ui db is closed - reestablishing database connection")
        try:
            db.connect()
        except Exception as e:
            logging.error("before_request exception %s", e)
            logging.error("db:\n%s", db)
    else:
        logging.info("db connection is allegedly healthy")
    # try:
    #     db = connect(os.environ['DATABASE_URL'], autorollback=True)
    # except Exception as e:
    #     logging.error("before_request exception %s", e)

# @app.teardown_request
# def teardown_request():
#     db.close()


@app.before_serving
async def before_serving():
    """pre"""
    logging.debug("before_serving")


@app.route('/', methods=['GET'])
async def index():
    """show the now playing page"""
    spotifyid = request.cookies.get('spotifyid')
    if 'spotifyid' in session:
        spotifyid = session['spotifyid']
    else:
        return redirect("/auth")

    user, token = await getuser(spotifyid)
    logging.debug("user=%s", user)
    with spotify.token_as(token): # pylint disable=used-before-assignment
        currently = await spotify.playback_currently_playing()

    playhistory = await getrecents()

    if currently is None:
        np_id="no id"
        rsum = 0
        np_name=""
    else:
        np_id = currently.item.id
        np_name = await trackinfo(np_id)
        query = Rating.select().where(Rating.trackid == np_id)
        ratings = [x for x in iter(query)] # pylint disable=not-an-iterable
        rsum = sum([x.rating for x in ratings])

    run_tasks = os.getenv('RUN_TASKS', 'spotify_watcher queue_manager web_ui')

    tasknames = [x.get_name() for x in asyncio.all_tasks()]
    logging.debug("tasknames=%s", tasknames)

    if "spotify_watcher" in run_tasks:
        if f"watcher_{spotifyid}" in tasknames:
            logging.debug("watcher_%s is running", spotifyid)
        else:
            logging.info("trying to launch a watcher for %s", spotifyid)
            user_task = asyncio.create_task(spotify_watcher(spotifyid),
                            name=f"watcher_{spotifyid}")
            tasks.add(user_task) # pylint disable=used-before-assignment
            user_task.add_done_callback(tasks.remove(user_task))

    return await render_template('index.html',
                                 np_name=np_name,
                                 np_id=np_id,
                                 rating=rsum,
                                 history=playhistory)


@app.route('/auth', methods=['GET'])
async def spotify_authorization():
    """redirect user to spotify for authorization"""
    scope = [ "user-read-playback-state",
            "user-modify-playback-state",
            "user-read-currently-playing",
            "user-read-recently-played",
            "streaming",
            "app-remote-control",
            "user-library-read",
            "user-top-read",
            "playlist-read-private",
            ]
    auth = tk.UserAuth(cred, scope)
    logging.debug("auth=%s", auth)
    state = auth.state

    auths[state] = auth
    logging.info("auth_url=%s", auth.url)
    return await render_template('auth.html', spoturl=auth.url)


@app.route('/spotify/callback', methods=['GET','POST'])
async def spotify_callback():
    """create a user record and set up initial ratings"""

    # users = getactiveusers()
    code = request.args.get('code', "")
    state = request.args.get('state', "")
    logging.debug("state: %s", state)
    thisauth = auths.pop(state, None) # pylint disable=used-before-assignment

    if thisauth is None:
        return 'Invalid state!', 400

    token = thisauth.request_token(code, state)

    with spotify.token_as(token):
        spotify_user = await spotify.current_user()

    spotifyid = spotify_user.id
    session['spotifyid'] = spotifyid

    query = User.select().where(User.id == spotifyid)
    if query.exists():
        logging.info("spotify_callback - found user record for %s", spotifyid)
        user = User.get_or_none(id=spotify)
        logging.info("pulling ratings to populate user")
        asyncio.create_task(pullratings(spotifyid),name=f"pullratings_{spotifyid}")
    else:
        logging.info("spotify_callback - creating user record for %s", spotifyid)
        p = pickle.dumps(token)
        user = User.create(id=spotifyid,
                           token=p,
                           display_name=spotify_user.display_name,
                           href=spotify_user.href)
        logging.debug("user=%s", user)
        logging.info("pulling ratings to populate user")
        asyncio.create_task(pullratings(spotifyid),name=f"pullratings_{spotifyid}")

    logging.info("spotify_callback - redirecting back to / %s", spotifyid)
    return redirect("/")
    # return redirect(f"https://discord.com/channels/{SERVER}/{CHANNEL}", 307)


@app.route('/dash', methods=['GET'])
async def dashboard():
    """show what's happening"""
    # ratings = [x for x in Rating.select()]

    # queued = [await trackinfo(trackid) for trackid in queue]
    try:
        dbqueue = UpcomingQueue.select()
    except Exception as e: # pylint: disable=W0718
        logging.error("dashboard - database queue retrieval exception: %s", e)

    tracknames = [Track.get_by_id(x).trackname for x in [x.trackid for x in dbqueue]]
    activeusers = getactiveusers()
    history = await getrecents()
    tasknames = [x.get_name() for x in asyncio.all_tasks() if "Task-" not in x.get_name()]
    return await render_template('dashboard.html',
                                 auths=auths,
                                 users=activeusers,
                                 tasks=tasknames,
                                 queue=tracknames,
                                 recents=history)


@app.route('/pullratings', methods=['GET'])
async def pullratings(spotifyid=None):
    """load up a bunch of ratings for a user"""
    if 'spotifyid' in session:
        spotifyid = session['spotifyid']
    else:
        return redirect("/auth")

    user, token = await getuser(spotifyid)
    logging.debug("user=%s", user)

    with spotify.token_as(token):
        # try:
        #     ratings = Rating.get(user_id=spotifyid)
        # except Exception as e:
        #     logging.error("rating error: %s", e)

        # rate recent history (20 items)
        r = [item.track.id async for item in
             spotify.all_items(await spotify.playback_recently_played())]
        # recents = await spotify.playback_recently_played()
        rated = await rate_list(r, spotifyid, 1)

        # # rate tops
        # tops = await spotify.current_user_top_tracks()
        # tops = await spotify.all_items(await spotify.current_user_top_tracks())
        # rated = rated + await rate_list(tops, spotifyid, 4)

        saved_tracks = [item.track.id async for item in
                        spotify.all_items(await spotify.saved_tracks())]
        rated = rated + await rate_list(saved_tracks, spotifyid, 4)

        message = f"rated {rated} items"
        logging.info(message)

        return redirect("/")


# @app.route('/watch', methods=['GET'])
# async def watch(spotifyid):
#     """start a watcher for a user"""
#     run_tasks = os.getenv('RUN_TASKS', 'spotify_watcher queue_manager web_ui')
#     if "spotify_watcher" not in run_tasks:

#     else:
#         tasknames = [x.get_name() for x in asyncio.all_tasks()]
#         logging.debug("tasknames=%s", tasknames)
#         if f"watcher_{spotifyid}" in tasknames:
#             logging.debug("watcher_%s is running", spotifyid)
#         else:
            # logging.info("trying to launch a watcher for %s", spotifyid)
            # user_task = asyncio.create_task(spotify_watcher(spotifyid),
            #                 name=f"watcher_{spotifyid}")
            # tasks.add(user_task) # pylint disable=used-before-assignment
            # user_task.add_done_callback(tasks.remove(user_task))

async def getuser(userid):
    """fetch user details"""
    user = User.get(User.id == userid)
    token = pickle.loads(user.token)
    if token.is_expiring:
        try:
            token = cred.refresh(token)
        except Exception as e:
            logging.error("exception: %s", e)
        user.token = pickle.dumps(token)
        user.save()

    return user, token


async def getrecents():
    """pull recently played tracks from history table"""
    ph_query = PlayHistory.select().order_by(PlayHistory.played_at.desc()).limit(10)
    playhistory = [await trackinfo(x.trackid) for x in ph_query]
    return playhistory


async def trackinfo(trackid, return_track=False, return_time=False):
    """pull track name (and details))

    Args:
        trackid (str): Spotify's unique track id
        return_track (bool, optional): also return the track. Defaults to False.
        return_time (bool, optional): also return the track duration

    Returns:
        str: track artist and title
        str, track object: track artist and title, track object
    """
    track = await spotify.track(trackid)
    artist = " & ".join([x.name for x in track.artists])
    name = f"{artist} - {track.name}"

    if return_time:
        milliseconds = track.duration_ms
        seconds = int(milliseconds / 1000) % 60
        minutes = int(milliseconds / (1000*60)) % 60
        name = f"{name} {minutes}:{seconds:02}"

    if return_track is True:
        return name, track
    else:
        return name


async def rate_list(items, uid, rating=1):
    """rate a bunch of stuff at once"""
    if isinstance(items, list):
        if isinstance(items[0], str):
            trackids = items
        else:
            trackids = [x.id for x in items]
    else:
        trackids = [x.track.id for x in items]
    logging.info("rating %s tracks", len(trackids))

    with db.atomic():
        for tid in trackids:
            await rate(uid, tid, rating)

    return len(trackids)


async def rate(uid, tid, value=1, set_last_played=True):
    """rate a track"""
    logging.info("checking the database for a track: %s", tid)
    try:
        track = Track.get_by_id(tid)
    except Exception as _: # pylint: disable=broad-exception-caught
        trackname = await trackinfo(tid)
        logging.info("adding a track to database: %s - %s", tid, trackname)
        try:
            track = Track(
                trackid=tid,
                trackname=trackname)
            track.save(force_insert=True)
        except IntegrityError as ie:
            logging.error("rate - error: %s", ie)

    logging.info("writing a rating: %s %s %s", uid, track.trackname, value)

    if set_last_played:
        _ = (Rating
                .replace(user_id=uid,
                         trackid=track.trackid,
                         trackname=track.trackname,
                         rating=value)
                .on_conflict(
                    conflict_target=[Rating.user_id, Rating.trackid],
                    preserve=[Rating.user_id, Rating.trackid],
                    update={Rating.last_played: datetime.datetime.now()}) # pylint disable=E1101
                .execute())

    else:
        _ = (Rating
            .replace(user_id=uid, trackid=track.trackid,
                     rating=value, trackname=track.trackname, last_played="1970-01-01")
            .on_conflict(
                conflict_target=[Rating.user_id, Rating.trackid],
                preserve=[Rating.user_id, Rating.trackid],
                update={Rating.last_played: "1970-01-01"})
            .execute())


async def record(uid, tid):
    """write a record to the play history table"""
    trackname= await trackinfo(tid)
    logging.info("play history recorded:  %s - %s", uid, trackname)
    try:
        insertedkey = PlayHistory.get_or_create(
            trackid=tid, played_at=datetime.datetime.now()) # pylint disable=E1101
        logging.debug("inserted play history record %s", insertedkey)
    except IntegrityError as e:
        logging.error("couldn't get/create history: %s - %s", uid, e)


async def getnext():
    """get the next trackid and trackname from the queue"""
    logging.debug("pulling queue from db")
    selector = UpcomingQueue.select().order_by(UpcomingQueue.id)
    dbqueue = [x.trackid.trackid for x in iter(selector)]
    logging.debug("queue pulled, %s items", len(dbqueue))
    if len(dbqueue) < 1:
        logging.debug("queue is empty, returning None")
        return None

    nextup_tid = dbqueue[0]
    ntrack = Track.get_by_id(nextup_tid)
    nextup_name = ntrack.trackname
    return nextup_tid, nextup_name


def recently_played_tracks():
    """fetch"""
    interval = 5
    # _ = getactiveusers()
    timeout = SQL(f"current_timestamp - interval '{interval} hours'")
    selector = Rating.select().distinct(Rating.trackid).where(Rating.last_played > timeout)
    # selector = Rating.select().where(Rating.last_played > timeout)
    tids = [x.trackid for x in selector]
    return tids


def getactiveusers():
    """fetch details for the active users"""
    return User.select()


async def spotify_watcher(userid):
    """start a long-running task to monitor a user's spotify usage, rate and record plays"""

    procname = f"watcher_{userid}"
    logging.info("starting a spotify watcher: %s", procname)

    try:
        _, token = await getuser(userid)
    except Exception as e: # pylint: disable=broad-exception-caught
        logging.error("%s getuser exception %s",procname, e)

    playing_tid = ""
    ttl = datetime.datetime.now() + datetime.timedelta(minutes=20)
    localhistory = []

    # Check the current status
    with spotify.token_as(token):
        try:
            currently = await spotify.playback_currently_playing()
        except Exception as e: # pylint: disable=broad-exception-caught
            logging.error("%s spotify_currently_playing exception %s", procname, e)
        if currently is None:
            logging.debug("%s not currently playing", procname)
            sleep = 30
        else:
            # previous_tid = None
            playing_tid = currently.item.id
            try:
                trackname = await trackinfo(playing_tid)
            except Exception as e: # pylint: disable=broad-exception-caught
                logging.error("exception %s", e)

            remaining_ms = currently.item.duration_ms - currently.progress_ms
            seconds = int(remaining_ms / 1000) % 60
            minutes = int(remaining_ms / (1000*60)) % 60
            logging.info("%s initial status - playing %s, %s:%0.02d remaining",
                         procname, trackname, minutes, seconds)

    # Loop while alive
    logging.debug("%s starting loop", procname)
    while ttl > datetime.datetime.now(): # pylint: disable=E1101
        status = "unset"
        logging.debug("%s loop is awake", procname)

        with spotify.token_as(token):
            logging.debug("%s checking currently playing", procname)
            try:
                currently = await spotify.playback_currently_playing()
            except Exception as e: # pylint: disable=W0718
                logging.error("exception %s",e)

            logging.debug("%s checking player queue state", procname)
            playbackqueue = await spotify.playback_queue()
            playbackqueueids = [x.id for x in playbackqueue.queue]

            if currently is None:
                status = "not playing"
                logging.debug("%s not currently playing", procname)
                sleep = 30
            elif currently.is_playing is False:
                status = "paused"
                logging.debug("%s is paused", procname)
                sleep = 30
            else:
                logging.debug("%s updating ttl: %s", procname, ttl)
                ttl = datetime.datetime.now() + datetime.timedelta(minutes=20)

                trackid = currently.item.id
                if trackid not in localhistory:
                    localhistory.append(trackid)
                trackname = await trackinfo(trackid)
                remaining_ms = currently.item.duration_ms - currently.progress_ms
                seconds = int(remaining_ms / 1000) % 60
                minutes = int(remaining_ms / (1000*60)) % 60

                nextup_tid, nextup_name = await getnext()

                if remaining_ms > 30000:
                    if (remaining_ms - 30000) < 30000:
                        sleep = (remaining_ms - 30000) / 1000
                    else:
                        sleep = 30

                elif remaining_ms <= 30000:
                    logging.info("%s LAST 30 SECONDS IN TRACK - %s",
                                procname, trackname)

                    # we got to the end of the track, so record a +1 rating
                    value = 1
                    logging.info("%s setting a rating, %s %s %s", userid, trackid, value, procname)
                    await rate(userid, trackid, value)

                    # if we're finishing the Currently Playing queued track
                        # remove it from the queue
                        # record it in the play history
                    if trackid == nextup_tid:

                        logging.info("%s removing track from radio queue: %s",
                                    procname, nextup_name)
                        try:
                            d = UpcomingQueue.delete().where(UpcomingQueue.trackid==nextup_tid)
                            d.execute()
                        except Exception as e: # pylint: disable=W0718
                            logging.error("exception - %s", e)

                        logging.info("%s recording a play history %s",
                                    procname, trackname)
                        await record(userid, trackid)

                    # get the next queued track
                    nextup_tid, nextup_name = await getnext()
                    if nextup_tid in playbackqueueids:
                        # this next track is already in the queue (or context, annoyingly)
                        # just sleep until this track is done
                        logging.debug("%s next track already queued, don't requeue",
                                    procname)
                        # sleep = (remaining_ms /1000) + 1
                    elif nextup_tid == trackid:
                        logging.debug("%s next track currently playing, don't requeue",
                                    procname)
                    else:
                        # dbtrack = Track.get_by_id(nextup_tid)
                        # get the track details with the track uri

                        logging.info("%s - fetching track details from spotify for %s",
                                        procname, nextup_name)
                        try:
                            track = await spotify.track(nextup_tid)
                        except Exception as e: # pylint: disable=W0718
                            logging.error("exception: %s", e)

                        # queue up the next track for this user
                        logging.info("%s sending to spotify client queue %s", procname, nextup_name)
                        try:
                            _ = await spotify.playback_queue_add(track.uri)
                        except Exception as e: # pylint: disable=W0718
                            logging.error("%s %s exception - " +
                                            "failed sending a track to the spotify queue: %s\n%s",
                                            procname, type(e), nextup_name, e)

                        sleep = (remaining_ms /1000) + 1

                status = f"{trackname} {minutes}:{seconds:0>2} remaining"
                # logging.info("%s playing %s %s:%0.02d remaining",
                    #   procname, trackname, minutes, seconds)

        if status == "not playing":
            logging.debug("%s sleeping %0.2ds - %s", procname, sleep, status)
        else:
            logging.info("%s sleeping %0.2ds - %s", procname, sleep, status)
        await asyncio.sleep(sleep)

    logging.info("%s timed out, watcher exiting", procname)


async def queue_manager():
    """manage the queue"""
    procname = "queue_manager"
    sleep = 10 # ten seconds between loops
    logging.info('%s starting', procname)

    while True:
        logging.debug("%s checking queue state", procname)

        if db.is_closed():
            db.connect()

        query = UpcomingQueue.select().order_by(UpcomingQueue.id)
        try:
            uqueue = [x.trackid for x in iter(query)]
        except Exception as e: # pylint: disable=W0718
            logging.error("%s, %s exception, failed pulling queue from database\n%s",
                          procname, type(e), e)

        while len(uqueue) > 2:
            newest = uqueue.pop()
            logging.info("%s queue is too large, removing latest trackid %s",
                         procname, newest)
            d = UpcomingQueue.delete().where(UpcomingQueue.trackid==newest)
            d.execute()

        while len(uqueue) < 2:
            logging.info("%s queue is too small, adding a track", procname)

            recent_tids = recently_played_tracks()
            logging.info("%s pulled %s recently played tracks", procname, len(recent_tids))

            selector = Rating.select(
                        Rating.trackid).group_by(
                        Rating.trackid).having(
                        fn.Sum(Rating.rating) > 0).order_by(fn.Random()).limit(50)
            positive_tracks = [x.trackid for x in selector]
            logging.info("%s pulled %s positive_tracks", procname, positive_tracks)

            potentials = [x for x in positive_tracks if x not in recent_tids + uqueue]
            if len(potentials) == 0:
                logging.info("%s no potential tracks to queue, sleeping for 60 seconds", procname)
                await asyncio.sleep(60)
                continue
            else:
                logging.info("%s %s potential tracks to queue", procname, len(potentials))
            # for each in recommendations:

                # if each not in potentials:
                    # potentials.append(each)

            upcoming_tid = choice(potentials)
            # result = await trackinfo(upcoming_tid, return_track=True)

            track = Track.get_by_id(upcoming_tid)
            ratings = Rating.select().where(Rating.trackid==upcoming_tid)
            for r in iter(ratings):
                logging.info("RATING HISTORY - %s, %s, %s, %s",
                             r.trackname, r.user_id, r.rating, r.last_played)
            logging.info("adding to radio queue: %s %s", upcoming_tid, track.trackname)
            _ = UpcomingQueue.create(trackid=upcoming_tid)
            uqueue.append(upcoming_tid)

            # ttl[upcoming_tid] = time.time() + (upcoming_track.duration_ms/1000)
            # h = [x for x in PlayHistory.select().where(PlayHistory.trackid == upcoming_tid)]
            # r = [x for x in Rating.select().where(Rating.trackid == upcoming_tid)]
            # sr = sum([x.rating for x in r])
            # if len(h) == 0:
            #     lp = "never played"
            # else:
            #     lp = h[0].played_at


            # logging.info(f"{procname} queued: {upcoming_name} [{sr} - {lp}]
            # of {len(potentials)} potential songs")
            # for each in h:
            #     logging.info(f"{procname} played at: {each.played_at}")
            # for each in r:
            #     logging.info(f"{procname} rating: {each.user_id}
            # [{each.rating}] ({each.last_played})")

        logging.debug("%s sleeping for %s", procname, sleep)
        await asyncio.sleep(sleep)


async def main():
    """kick it"""
    taskset = set()
    logging.info("connecting to db")
    db.connect()
    db.create_tables([User, Rating, PlayHistory, Track, UpcomingQueue])

    run_tasks = os.getenv('RUN_TASKS', 'spotify_watcher queue_manager web_ui')
    logging.info("running tasks: %s", run_tasks)

    if "spotify_watcher" in run_tasks:
        active_users = getactiveusers()
        for user in active_users:
            logging.info("creating a spotify watcher task for: %s", user.id)
            user_task = asyncio.create_task(spotify_watcher(user.id),
                            name=f"watcher_{user.id}")

            # add this user task to the global tasks set
            taskset.add(user_task)

            # To prevent keeping references to finished tasks forever,
            # make each task remove its own reference from the set after
            # completion:
            user_task.add_done_callback(taskset.remove(user_task))

    if "queue_manager" in run_tasks:
        logging.info("creating a queue manager task")
        qm = asyncio.create_task(queue_manager(),name="queue_manager")
        taskset.add(qm)
        qm.add_done_callback(taskset.remove(qm))

    if "web_ui" in run_tasks:
        logging.info("starting web_ui on port: %s", os.environ['PORT'])
        web_ui = app.run_task('0.0.0.0', os.environ['PORT'])
        taskset.add(web_ui)

    try:
        await asyncio.gather(*taskset)
    except Exception as e:
        logging.error("exception %s", e)

    await asyncio.gather(*asyncio.all_tasks())
    logging.info("main done")


if __name__ == "__main__":
    secret=os.environ['SPOTIFY_CLIENT_SECRET']
    logging.info("SPOTIFY_CLIENT_ID=%s", os.environ['SPOTIFY_CLIENT_ID'])
    logging.info("SPOTIFY_CLIENT_SECRET=%s...%s", secret[-2:], secret[:2])
    logging.info("SPOTIFY_REDIRECT_URI=%s", os.environ['SPOTIFY_REDIRECT_URI'])

    asyncio.run(main())
