#!/usr/bin/env python
# from distutils.log import error
# from multiprocessing import set_forkserver_preload
from datetime import datetime, timedelta
import logging
import os
# import signal
# import sys
from collections import deque
import asyncio
import pickle
import tekore as tk
from random import choice
from dotenv import load_dotenv
from quart import Quart, request, redirect, render_template, session
# make_response
from peewee import *
# import time
from playhouse.db_url import connect

load_dotenv()  # take environment variables from .env

db = connect(os.environ['DATABASE_URL'], autorollback=True)

app = Quart(__name__)
app.secret_key = "flumple"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt="%Y-%m-%d %H:%M:%S"
    )

class BaseModel(Model):
    """A base model that will use our database"""
    class Meta:
        database = db


class User(BaseModel):
    id = TextField(primary_key=True)
    token = BlobField()
    last_active = DateTimeField(constraints=[SQL('DEFAULT CURRENT_TIMESTAMP')])


class Track(BaseModel):
    trackid = CharField(primary_key=True)
    trackname = TextField()


class Rating(BaseModel):
    user_id = ForeignKeyField(User, backref="ratings")
    # trackid = ForeignKeyField(Track, backref="trackid")
    trackid = CharField()
    trackname = TextField()
    rating = IntegerField()
    last_played = DateTimeField(constraints=[SQL('DEFAULT CURRENT_TIMESTAMP')])
    
    class Meta:
        indexes = (
            (('user_id', 'trackid'), True),
        )


class PlayHistory(BaseModel):
    trackid = CharField()
    played_at = TimestampField()

    class Meta:
        indexes = (
            (('trackid', 'played_at'), True),
        )


@app.before_request
def make_session_permanent():
    session.permanent = True


@app.before_serving
async def before_serving():
    """pre"""
    logging.info("before_serving")


@app.route('/', methods=['GET'])
async def index():
    """show the now playing page"""
    global tasks
    spotifyid = request.cookies.get('spotifyid')
    if 'spotifyid' in session:
        spotifyid = session['spotifyid']
    else:
        return redirect("/auth")

    user, token = await getuser(spotifyid)
    logging.debug("user=%s", user)
    with spotify.token_as(token):
        currently = await spotify.playback_currently_playing()
    
    if currently is None:
        return await render_template('index.html',
                                     np_name="Nothing Playing",
                                     np_id="no id", rating=0, history=[])
    
    tasknames = [x.get_name() for x in asyncio.all_tasks()]
    logging.debug("tasknames=%s", tasknames)
    if f"watcher_{spotifyid}" in tasknames:
        logging.debug("already running a watcher for %s, not launching another", spotifyid)
    else:
        logging.info("trying to launch a watcher for %s", spotifyid)
        t = asyncio.create_task(spotify_watcher(spotifyid),
                                         name=f"watcher_{spotifyid}")
        tasks.append(t)

    np_id = currently.item.id
    np_name = await trackinfo(np_id)
    query = Rating.select().where(Rating.trackid == np_id)
    ratings = [x for x in query]
    rsum = sum([x.rating for x in ratings])

    ph_query = PlayHistory.select().where(PlayHistory.trackid != np_id).order_by(PlayHistory.played_at.desc()).limit(10)
    # playhistory = [x.played_at.strftime('%H:%M:%S %Y-%m-%d')
    playhistory = [await trackinfo(x.trackid) for x in ph_query]
    return await render_template('index.html',
                                 np_name=np_name,
                                 np_id=np_id,
                                 rating=rsum,
                                 history=playhistory)


@app.route('/spotify/callback', methods=['GET','POST'])
async def spotify_callback():
    global auths
    users = await getactiveusers()
    code = request.args.get('code', "")
    state = request.args.get('state', "")
    logging.debug("state: %s", state)
    thisauth = auths.pop(state, None)

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


@app.route('/auth', methods=['GET'])
async def auth():    
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
    logging.info("auth=%s", auth)
    state = auth.state

    auths[state] = auth
    logging.info("auth_url=%s", auth.url)
    return await render_template('auth.html', spoturl=auth.url)


@app.route('/dash', methods=['GET'])
async def dashboard():
    # ratings = [x for x in Rating.select()]

    queued = [await trackinfo(trackid) for trackid in queue]
    users = await getactiveusers()
    history = await getrecents()
    tasks = [x.get_name() for x in asyncio.all_tasks()]
    return await render_template('dashboard.html',
                                 auths=auths,
                                 users=users,
                                 tasks=tasks,
                                 queue=queued,
                                 recommendations=recommendations,
                                 recents=history)


@app.route('/pullratings', methods=['GET'])
async def pullratings(spotifyid=None):
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
        r = [item.track.id async for item in spotify.all_items(await spotify.playback_recently_played())]
        # recents = await spotify.playback_recently_played()
        rated = await rate_list(r, spotifyid, 1)
        
        # # rate tops
        # tops = await spotify.current_user_top_tracks()
        # tops = await spotify.all_items(await spotify.current_user_top_tracks())
        # rated = rated + await rate_list(tops, spotifyid, 4)

        saved_tracks = [item.track.id async for item in spotify.all_items(await spotify.saved_tracks())]
        rated = rated + await rate_list(saved_tracks, spotifyid, 4)

        message = f"rated {rated} items"
        logging.info(message)

        return redirect("/")


async def getuser(userid):
    """fetch user details"""
    user = User.get(User.id == userid)
    token = pickle.loads(user.token)
    if token.is_expiring:
        token = cred.refresh(token)
        user.token = pickle.dumps(token)
        user.save()

    return user, token


async def getrecents():
    ph_query = PlayHistory.select().order_by(PlayHistory.played_at.desc()).limit(10)
    playhistory = [await trackinfo(x.trackid) for x in ph_query]
    return playhistory


async def getactiveusers():
    """fetch details for the active users"""
    # todo - select only active users, expire others
    return User.select()


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
    # except Exception as e:
        # logging.error("-------------- couldn't retrieve track from spotify: %s error=%s", trackid, e)
    
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
    if type(items) is list:
        if type(items[0]) is str:
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
    logging.debug("checking the database for a track: %s", tid)
    try:
        track, _created = Track.get_or_create(
            trackid=tid,
            defaults={'trackname': ""})
    except IntegrityError as e:
        logging.error("rate - error: %s", e)

    if track.trackname == "":
        logging.debug("checking spotify for track details: %s", tid)
        trackname = await trackinfo(tid)
        logging.info("adding a track to database: %s - %s", tid, trackname)
        track.trackname = trackname
        track.save()
    else:
        trackname = track.trackname

    logging.info("writing a rating: %s %s %s", uid, trackname, value)
    # try:
    if set_last_played:
        rating = (Rating
                .replace(user_id=uid, trackid=tid, trackname=trackname, rating=value)
                .on_conflict(
                    conflict_target=[Rating.user_id, Rating.trackid],
                    preserve=[Rating.user_id, Rating.trackid],
                    update={Rating.last_played: datetime.now()})
                .execute())
    else:
        rating = (Rating
            .replace(user_id=uid, trackid=tid, 
                     rating=value, trackname=trackname, last_played="1970-01-01")
            .on_conflict(
                conflict_target=[Rating.user_id, Rating.trackid],
                preserve=[Rating.user_id, Rating.trackid],
                update={Rating.last_played: "1970-01-01"})
            .execute())
    logging.debug("rating=%s", rating)
    # except Exception as e:
    #     logging.error("{rating error: uid=%s error=%s", uid, e)


async def record(uid, tid):
    """write a record to the play history table"""
    trackname= await trackinfo(tid)
    logging.info("play history recorded:  %s - %s", uid, trackname)
    try:
        insertedkey = PlayHistory.get_or_create(trackid=tid, played_at=datetime.now())
        logging.debug("inserted play history record %s", insertedkey)
    except IntegrityError as e:
        logging.error("couldn't get/create history: %s - %s", uid, e)


async def spotify_watcher(userid):
    """start a long-running task to monitor a user's spotify usage, rate and record plays"""
    logging.info("starting a spotify watcher")
    global recommendations
    global tasks
    logging.info("setting the procname")
    procname = f"watcher_{userid}"
    logging.info("procname: %s", procname)

    # todo - check for an existing live watcher for this user
    
    logging.info("starting a spotify watcher: %s", procname)

    user, token = await getuser(userid)
    logging.debug("user=%s", user)
    playing_tid = ""
    ttl = datetime.now() + timedelta(minutes=20)
    localhistory = []

    # Check the current status
    with spotify.token_as(token):
        currently = await spotify.playback_currently_playing()
        if currently is None:
            logging.info("%s not currently playing", procname)
            sleep = 30
        else:
            # previous_tid = None
            playing_tid = currently.item.id
            trackname = await trackinfo(playing_tid)
            # nextup_tid = queue[0]
            
            remaining_ms = currently.item.duration_ms - currently.progress_ms
            seconds = int(remaining_ms / 1000) % 60
            minutes = int(remaining_ms / (1000*60)) % 60
            logging.info("%s playing %s %s:%0.2s remaining", procname, trackname, minutes, seconds)
            
            logging.info("%s pulling spotify recommendations", procname)
            r = await spotify.recommendations(track_ids=[playing_tid])
            recommendations += [item.id for item in r.tracks]
            logging.info("%s found %s recommendations", procname, len(recommendations))
    
    # Loop while alive
    while ttl > datetime.now():
        logging.debug("%s awake", procname)
        
        with spotify.token_as(token):
            currently = await spotify.playback_currently_playing()
            if currently is None:
                logging.info("%s not currently playing", procname)
                return
            else:
                # TODO add a token refresh to the ttl check

                logging.debug("%s updating ttl: %s", procname, ttl)
                ttl = datetime.now() + timedelta(minutes=20)

            trackid = currently.item.id
            trackname = await trackinfo(trackid)
            remaining_ms = currently.item.duration_ms - currently.progress_ms
            seconds = int(remaining_ms / 1000) % 60
            minutes = int(remaining_ms / (1000*60)) % 60

            if len(queue) < 1:
                logging.info("queue is empty, skipping %s", userid)
                await asyncio.sleep(10)
                continue
                
            nextup_tid = queue[0]
            nextup_name = await trackinfo(nextup_tid)

            # do logic to see if lastplayed finished?
            # logging.info(f"{procname} started playing {trackname} {remaining_ms/1000}s remaining")

            if trackid == nextup_tid:
                logging.debug("%s popping queue, next up is same as currently playing: %s", procname, nextup_name)
                queue.popleft()
                await record(userid, trackid)

            if remaining_ms > 30000:
                if (remaining_ms - 30000) < 30000:
                    sleep = (remaining_ms - 30000) / 1000
                else:
                    sleep = 30

            elif remaining_ms <= 30000:
                # if nextup_tid in localhistory:
                    # logging.warning("don't requeue something: %s", nextup_name)
                    # sleep = 5
                
                # else:
                logging.info("30 seconds remaining in track, saving a rating %s %s 1", userid, trackid)
                await rate(userid, trackid, 1)

                # try:
                logging.info("sending to spotify client queue %s %s", procname, nextup_name)
                track = await spotify.track(nextup_tid)
                result = await spotify.playback_queue_add(track.uri)
                logging.debug("result=%s", result)
                localhistory.append(nextup_tid)
                sleep = (remaining_ms /1000) + 1
                    # except Exception as e:
                    #     logging.error("%s queuing error: %s", procname, e)
                    #     sleep = 5

                logging.debug(procname)

        logging.info("%s playing %s %s:%0.02d remaining, sleeping for %ss",
                      procname, trackname, minutes, seconds, sleep)
        await asyncio.sleep(sleep)

    logging.info("%s timed out, watcher exiting", procname)


def recently_played_tracks():
    """fetch"""
    interval = 5
    # timeout = SQL(f"current_timestamp - INTERVAL '{interval} days'")
    # selector = Rating.select().distinct(Rating.trackid).where(Rating.user_id in [u for u in users]).where(Rating.last_played > timeout)
    # tids = [x.trackid for x in selector]
    tids = []
    return tids


async def queue_manager():
    """manage the queue"""
    procname = "queue_manager"
    logging.info('%s starting', procname)
    # ttl = {}

    while True:

        while len(queue) < 2:

            rated_tracks = [x.trackid for x in Rating.select()]
            # unplayed_good_tracks = [x.trackid for x in Rating.select(Rating.trackid).where(Rating.user_id in [x for x in users]).group_by(Rating.trackid).having(fn.Sum(Rating.rating) > 0).order_by(fn.Random()).limit(50)]
            # recent_tids = recently_played_tracks()

            # potentials = [x for x in unplayed_good_tracks if x not in recent_tids]
            potentials = rated_tracks
            if len(potentials) == 0:
                await asyncio.sleep(60)
                continue
            # for each in recommendations:

                # if each not in potentials:
                    # potentials.append(each)

            upcoming_tid = choice(potentials)
            # result = await trackinfo(upcoming_tid, return_track=True)
            # (upcoming_name, upcoming_track) = result

            queue.append(upcoming_tid)

            # ttl[upcoming_tid] = time.time() + (upcoming_track.duration_ms/1000)
            # h = [x for x in PlayHistory.select().where(PlayHistory.trackid == upcoming_tid)]
            # r = [x for x in Rating.select().where(Rating.trackid == upcoming_tid)]
            # sr = sum([x.rating for x in r])
            # if len(h) == 0:
            #     lp = "never played"
            # else:
            #     lp = h[0].played_at


            # logging.info(f"{procname} queued: {upcoming_name} [{sr} - {lp}] of {len(potentials)} potential songs")
            # for each in h:
            #     logging.info(f"{procname} played at: {each.played_at}")
            # for each in r:
            #     logging.info(f"{procname} rating: {each.user_id} [{each.rating}] ({each.last_played})")
        await asyncio.sleep(10)


async def main():
    """kick it"""

    logging.info("connecting to db")
    db.connect()
    db.create_tables([User, Rating, PlayHistory, Track])

    active_users = await getactiveusers()
    for user in active_users:
        task = asyncio.create_task(spotify_watcher(user.id),
                            name=f"watcher_{user.id}")
        tasks.append(task)
        task.add_done_callback(tasks.remove(task))

    queue_man = asyncio.create_task(queue_manager(),name="queue_manager")
    tasks.append(queue_man)
    queue_man.add_done_callback(tasks.remove(queue_man))

    web_ui = app.run_task('0.0.0.0', os.environ['PORT'])
    tasks.append(web_ui)

    logging.info("Port: %s", os.environ['PORT'])
    await asyncio.gather(queue_man, web_ui, *tasks)


if __name__ == "__main__":
    users = []
    auths = {}  # Ongoing authorisations: state -> UserAuth - what does this mean?
    tasks = []
    queue = deque()
    recommendations = []

    conf = tk.config_from_environment()
    cred = tk.Credentials(*conf)
    token_spotify = tk.request_client_token(*conf[:2])

    secret=os.environ['SPOTIFY_CLIENT_SECRET']
    logging.info("SPOTIFY_CLIENT_ID=%s", os.environ['SPOTIFY_CLIENT_ID'])
    logging.info("SPOTIFY_CLIENT_SECRET=%s...%s", secret[-2:], secret[:2])
    logging.info("SPOTIFY_REDIRECT_URI=%s", os.environ['SPOTIFY_REDIRECT_URI'])

    spotify = tk.Spotify(token_spotify, asynchronous=True)

    asyncio.run(main())
