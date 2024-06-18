#!/usr/bin/env python
"""mercury radio"""
import datetime
import logging
import os
import asyncio
import pickle
import tekore as tk
from dotenv import load_dotenv
from quart import Quart, request, redirect, render_template, session
from tortoise.contrib.quart import register_tortoise
from models import User, Track, Rating, PlayHistory, UpcomingQueue
from watchers import user_reaper, watchman
from users import getactiveusers, getuser
from queue_manager import queue_manager, trackinfo

# pylint: disable=W0718,global-statement
# pylint: disable=broad-exception-caught
# pylint: disable=trailing-whitespace

load_dotenv()  # take environment variables from .env

app = Quart(__name__)
app.secret_key = os.getenv("APP_SECRET", default="1234567890")

conf = tk.config_from_environment()
cred = tk.Credentials(*conf)

token_spotify = tk.request_client_token(*conf[:2])
spotify = tk.Spotify(token_spotify, asynchronous=True)

taskset = set()
auths = {}

logging.basicConfig(
    level=logging.INFO,
    # format='%(asctime)s %(name)s %(module)s %(funcName)s %(levelname)s %(message)s',
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt="%Y-%m-%d %H:%M:%S"
    )

httpx_logger = logging.getLogger('httpx')
httpx_logger.setLevel(os.getenv("LOGLEVEL_HTTPX", default="WARN"))

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


register_tortoise(
    app,
    db_url=os.environ['DATABASE_URL'],
    modules={"models": ["models"]},
    generate_schemas=False,
)


@app.before_serving
async def before_serving():
    """pre"""
    procname="before_serving"

    run_tasks = os.getenv('RUN_TASKS', 'spotify_watcher queue_manager web_ui')
    logging.info("before_serving running tasks: %s", run_tasks)
    
    if "queue_manager" in run_tasks:
        logging.info("before_serving creating a queue manager task")
        qm = asyncio.create_task(queue_manager(spotify),name="queue_manager")
        taskset.add(qm)
        qm.add_done_callback(taskset.remove(qm))

    if "spotify_watcher" in run_tasks:
        
        if os.getenv("OVERTAKE") is True or 1:
            logging.warning("%s overtaking any existing watcher tasks", 
                        procname)    
            await User.select_for_update().exclude(watcherid='').update(watcherid='')

        logging.info("%s launching a user_reaper task", procname)
        reaper_task = asyncio.create_task(user_reaper(), name="user_reaper")
        taskset.add(reaper_task)
        reaper_task.add_done_callback(taskset.remove(reaper_task))
        
        # give the reaper a couple seconds to clean out inactive users
        await asyncio.sleep(2)
        
        logging.info("%s pulling active users for spotify watchers", procname)
        active_users = await getactiveusers()
        for user in active_users:
            await watchman(taskset, spotify_watcher, userid=user.spotifyid)

    


@app.before_request
def before_request():
    """save cookies even if you close your browser"""
    session.permanent = True


@app.route('/', methods=['GET'])
async def index():
    """show the now playing page"""
    procname="web_index"
    spotifyid = session.get('spotifyid', "login")
    if spotifyid == "":
        spotifyid = "login"
    displayname = ""
    np_name = ''
    np_id = ''
    rsum = ''
    targetid = ''
    
    # get play history
    playhistory = await getrecents()
    
    # get a list of active user ids
    displaynames = [x.displayname for x in await getactiveusers()]
    

    if spotifyid is not None and spotifyid != '' and spotifyid != 'login':

        # get user details
        user, token = await getuser(cred, spotifyid)
        displayname = user.displayname
        if displayname is None:
            displayname = ""
        
        # are we following somebody?
        if user.status.startswith("following"):
            logging.info("%s user.status=%s", procname, user.status)
            targetid = user.status.replace("following:", "")
            target = await User.get(spotifyid=targetid)
            token = pickle.loads(target.token)
            
        
        with spotify.token_as(token): # pylint disable=used-before-assignment
            currently = await spotify.playback_currently_playing()

        if currently is None or currently.is_playing is False:
            np_id="no id"
            rsum = 0
            np_name="Not Playing"
        else:
            np_id = currently.item.id
            np_name = await trackinfo(spotify, np_id)
            rsum = 0
            # rsum = ( await Rating.filter(trackid=np_id).values_list("rating", flat=True))
            
            # query = await Rating.filter(trackid=np_id).values_list('rating', flat=True)
            # ratings = [x for x in iter(query)]
            # rsum = sum([x.rating for x in ratings])

        run_tasks = os.getenv('RUN_TASKS', 'spotify_watcher queue_manager')

        tasknames = [x.get_name() for x in asyncio.all_tasks()]
        logging.debug("tasknames=%s", tasknames)

        if "spotify_watcher" in run_tasks:
            if f"watcher_{spotifyid}" in tasknames:
                logging.debug("watcher_%s is running", spotifyid)
            else:
                logging.info("trying to launch a watcher for %s", spotifyid)
                user_task = asyncio.create_task(spotify_watcher(spotifyid),
                                name=f"watcher_{spotifyid}")
                taskset.add(user_task) # pylint disable=used-before-assignment
                user_task.add_done_callback(taskset.remove(user_task))

    return await render_template('index.html',
                                 spotifyid=spotifyid,
                                 displayname=displayname,
                                 np_name=np_name,
                                 np_id=np_id,
                                 rating=rsum,
                                 targetid=targetid,
                                 activeusers=displaynames,
                                 history=playhistory
                                )


@app.route('/logout', methods=['GET'])
async def logout():
    """set a user as inactive and forget cookies"""
    spotifyid = request.cookies.get('spotifyid')
    if 'spotifyid' in session:
        spotifyid = session['spotifyid']
    else:
        return redirect("/")
    
    session['spotifyid'] = ""
    user = await User.get(spotifyid=spotifyid)
    user.status = "inactive"
    await user.save()
    return redirect("/")
    
    
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
            "user-follow-read",
            "playlist-read-private",
            ]
    auth = tk.UserAuth(cred, scope)
    logging.debug("auth=%s", auth)
    state = auth.state

    auths[state] = auth
    logging.debug("state: %s", state)
    logging.debug("auths: %s", auths)
    logging.debug("auth_url: %s", auth.url)
    return redirect(auth.url, 307)
    # return await render_template('auth.html', spoturl=auth.url)


@app.route('/spotify/callback', methods=['GET','POST'])
async def spotify_callback():
    """create a user record and set up initial ratings"""
    procname = "spotify_callback"

    # users = getactiveusers()
    code = request.args.get('code', "")
    state = request.args.get('state', "")
    logging.debug("state: %s", state)
    logging.debug("auths: %s", auths)
    thisauth = auths.pop(state, None)

    if thisauth is None:
        logging.error("%s couldn't find this state in auths: %s", procname, state)
        logging.error("auths: %s", auths)
        return redirect("/", 307)

    try:
        token = thisauth.request_token(code, state)
    except Exception as e:
        logging.error("%s exception request_token\n%s", procname, e)


    with spotify.token_as(token):
        try:
            spotify_user = await spotify.current_user()
        except Exception as e:
            logging.error("%s exception current_user\n%s", procname, e)
            return redirect("/")

    spotifyid = spotify_user.id
    session['spotifyid'] = spotifyid
    p = pickle.dumps(token)

    logging.info("spotify_callback get_or_create user record for %s", spotifyid)
    n = datetime.datetime.now(datetime.timezone.utc)
    user, created = await User.get_or_create(spotifyid=spotifyid,
                                             defaults={
                                                 "token": p,
                                                 "last_active": n,
                                                 "displayname": spotifyid,
                                                 "status": "active"
                                             })
    if created is False:
        logging.info('spotify_callback found user %s', spotifyid)
        user.last_active = datetime.datetime.now
        user.status = "active"
        await user.save()
    else:
        logging.info("spotify_callback creating new user %s", spotifyid)
        await user.save()             
        
        logging.info("spotify_callback pulling ratings to populate user")
        asyncio.create_task(pullratings(spotifyid),name=f"pullratings_{spotifyid}")
    
    logging.info("spotify_callback redirecting %s back to /", spotifyid)
    return redirect("/")


@app.route('/dash', methods=['GET'])
async def dashboard():
    """show what's happening"""
    # ratings = [x for x in Rating.select()]

    # queued = [await trackinfo(trackid) for trackid in queue]
    try:
        dbqueue = await UpcomingQueue.all().values_list('trackid', flat=True)
    except Exception as e: # pylint: disable=W0718
        logging.error("dashboard database queue retrieval exception: %s", e)

    tracknames = await Track.filter(trackid__in=dbqueue).values_list('trackname', flat=True)
    activeusers = await getactiveusers()
    history = await getrecents()
    tasknames = [x.get_name() for x in asyncio.all_tasks() if "Task-" not in x.get_name()]
    return await render_template('dashboard.html',
                                 auths=auths,
                                 username="now_playing",
                                 users=activeusers,
                                 tasks=tasknames,
                                 current=tracknames[0],
                                 nextup=tracknames[1],
                                 recents=history)


@app.route('/pullratings', methods=['GET'])
async def pullratings(spotifyid=None):
    """load up a bunch of ratings for a user"""
    if 'spotifyid' in session:
        spotifyid = session['spotifyid']
    else:
        return redirect("/auth")

    user, token = await getuser(cred, spotifyid)
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
        rated = await rate_list(r, spotifyid, 1, set_last_played=False)

        # # rate tops
        # tops = await spotify.current_user_top_tracks()
        # tops = await spotify.all_items(await spotify.current_user_top_tracks())
        # rated = rated + await rate_list(tops, spotifyid, 4)

        saved_tracks = [item.track.id async for item in
                        spotify.all_items(await spotify.saved_tracks())]
        rated = rated + await rate_list(saved_tracks, spotifyid, 4, set_last_played=False)

        message = f"rated {rated} items"
        logging.info(message)

        return redirect("/")


@app.route('/user', methods=['POST'])
async def user_update():
    """allow user profile updates"""
    procname = "user_update"
    
    logging.info("%s updating user record", procname)
    
    if 'spotifyid' not in session:
        logging.info("%s no spotify id in session", procname)
        return redirect("/", 303)
    
    if request.method == "POST":
        logging.info("%s fetching user %s", 
                     procname, session['spotifyid'])
        user, _ = await getuser(cred, session['spotifyid'])
        
        form = await request.form
        displayname = form['displayname']
        logging.info("%s updating displayname: %s",
                     procname, displayname)
        if displayname is not None:
            logging.info("%s updating displayname: %s",
                         procname, displayname)
            user.displayname = displayname
            await user.save()
            
    return redirect("/")


@app.route('/follow/<targetid>')
async def follow(targetid=None):
    """listen with a friend"""
    procname = "follow"
    if 'spotifyid' in session:
        if session['spotifyid'] == targetid:
            logging.warning("%s user %s tried to follow itself",
                            procname, targetid)
        else:
            user, _ = await getuser(cred, session['spotifyid'])
            user.status = "following:" + targetid
            
            logging.info("%s set user %s status: %s", 
                         procname, session['spotifyid'], user.status)
            await user.save()
    return redirect("/")


async def getrecents():
    """pull recently played tracks from history table"""
    try:
        ph_query = await PlayHistory.all().order_by('-id').limit(10)
    except Exception as e:
        logging.error("exception ph_query %s", e)

    try:
        playhistory = [await trackinfo(spotify, x.trackid) for x in ph_query]
    except Exception as e:
        logging.error("exception playhistory %s", e)

    return playhistory





async def rate_list(items, uid, rating=1, set_last_played=True):
    """rate a bunch of stuff at once"""
    if isinstance(items, list):
        if isinstance(items[0], str):
            trackids = items
        else:
            trackids = [x.id for x in items]
    else:
        trackids = [x.track.id for x in items]
    logging.info("rating %s tracks", len(trackids))

    for tid in trackids:
        await rate(uid, tid, rating, set_last_played=set_last_played)

    return len(trackids)


async def rate(uid, tid, value=1, set_last_played=True, autorate=False):
    """rate a track"""
    procname="rate"
    try:
        displayname, track = await trackinfo(spotify, tid, return_track=True)
    except Exception as e: # pylint: disable=broad-exception-caught
        logging.info("rate exception adding a track to database: [%s]\n%s",
                     tid, e)

    logging.info("%s writing a rating: %s %s %s", procname, uid, displayname, value)
    if set_last_played:
        lastplayed = datetime.datetime.now()
    else:
        lastplayed = "1970-01-01"
    rating, created = await Rating.get_or_create(userid=uid,
                                                trackid=tid,
                                                defaults={
                                                   "rating": value,
                                                   "trackname": track.trackname,
                                                   "last_played": lastplayed
                                                   }
                                               )

    # if the rating already existed, update the value and lastplayed time
    if not created:
        if rating.rating > value and autorate is True:
            logging.info("%s won't auto-downrate %s from %s to %s for user %s", 
                         procname, displayname, rating.rating, value, uid)
        else:
            logging.debug("%s writing a rating: %s %s %s",
                          procname, uid, displayname, value)
            rating.rating = value
    
    await rating.save()


async def record(uid, tid):
    """write a record to the play history table"""
    procname = "record"
    trackname = await trackinfo(spotify, tid)
    logging.info("%s play history %s %s", procname, uid, trackname)
    try:
        insertedkey = await PlayHistory.create(trackid=tid)
        await insertedkey.save()
    except Exception as e:
        logging.error("record exception creating playhistory: %s\n%s",
                      uid, e)
    
    logging.debug("record inserted play history record %s", insertedkey)


async def getnext():
    """get the next trackid and trackname from the queue"""
    logging.debug("pulling queue from db")
    dbqueue = await UpcomingQueue.all().order_by("id").values_list('trackid', flat=True)
    logging.debug("queue pulled, %s items", len(dbqueue))
    if len(dbqueue) < 1:
        logging.warning("queue is empty, returning None")
        return None, None

    nextup_tid = dbqueue[0]
    ntrack = await Track.get(trackid=nextup_tid)
    nextup_name = ntrack.trackname
    return nextup_tid, nextup_name


async def spotify_watcher(userid):
    """start a long-running task to monitor a user's spotify usage, rate and record plays"""

    procname = f"watcher_{userid}"
    
    logging.info("%s watcher starting", procname)

    try:
        user, token = await getuser(cred, userid)
    except Exception as e: # pylint: disable=broad-exception-caught
        logging.error("%s getuser exception %s",procname, e)

    # check for a lock in the database from another watcher
    recent = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1)
    timestamp = datetime.datetime.now().strftime('%s')
    watcherid = f"watcher_{user.spotifyid}_{timestamp}"
    
    if user.watcherid == "killswitch":
        logging.warning("%s detected killswitch, won't start, unsetting killswitch", procname)
        user.watcherid = ""
        await user.save()
        return "killswitch"
    elif (user.watcherid != ""
          and user.status != "inactive"
          and user.last_active > recent):
        logging.error("%s found another recent active watcher, won't start", procname)
        return "another active watcher"
    else:    
        user.watcherid = watcherid
        await user.save()
    
    # user.status = "active"
    # user.last_active = datetime.datetime.now(datetime.timezone.utc)
    # await user.save()
    
    ttl = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=20)
    localhistory = []

    # Check the initial status
    with spotify.token_as(token):
        try:
            currently = await spotify.playback_currently_playing()
        except Exception as e: # pylint: disable=broad-exception-caught
            logging.error("%s spotify_currently_playing exception %s", procname, e)
        
        if currently is None:
            logging.debug("%s not currently playing", procname)
            sleep = 30
            # track = None
        elif currently.is_playing is False:
            logging.debug("%s paused", procname)
            sleep = 30
            # track = None
        else:
            
            trackid = currently.item.id
            trackname, _ = await trackinfo(spotify, trackid, return_track=True)
            remaining_ms = currently.item.duration_ms - currently.progress_ms
            position = currently.progress_ms/currently.item.duration_ms
            
            seconds = int(remaining_ms / 1000) % 60
            minutes = int(remaining_ms / (1000*60)) % 60
            logging.info("%s initial status - playing %s, %s:%0.02d remaining",
                         procname, trackname, minutes, seconds)

    # Loop while alive
    logging.debug("%s starting loop", procname)
    while ttl > datetime.datetime.now(datetime.timezone.utc):
        
        logging.debug("%s loop is awake", procname)
        user = await User.get(spotifyid=userid)
        status = user.status
        
        if user.watcherid == "killswitch":
            logging.warning("%s detected killswitch, unsetting killswitch and exiting", procname)
            user.watcherid = ""
            await user.save()
            return "killswitch"
        elif (user.watcherid != watcherid 
          and user.status != "inactive"
          and user.last_active > recent):
            logging.error("%s found another recent active watcher, exiting", procname)
            return "another active watcher"
 
        if token.is_expiring:
            try:
                token = cred.refresh(token)
            except Exception as e:
                logging.error("getuser exception refreshing token\n%s", e)
            user.token = pickle.dumps(token)
            await user.save()

        with spotify.token_as(token):
            logging.debug("%s checking currently playing", procname)
            try:
                currently = await spotify.playback_currently_playing()
            except Exception as e:
                logging.error("%s exception in spotify.playback_currently_playing\n%s",procname, e)

            logging.debug("%s checking player queue state", procname)
            playbackqueue = await spotify.playback_queue()
            playbackqueueids = [x.id for x in playbackqueue.queue]

            if currently is None:
                status = "not playing"
                trackid = None
                logging.debug("%s not currently playing", procname)
                sleep = 30
            elif currently.currently_playing_type == "unknown":
                status = "not playing"
                trackid = None
                logging.debug("%s not currently playing", procname)
                sleep = 30
            elif currently.is_playing is False:
                status = "paused"
                trackid = currently.item.id
                logging.debug("%s is paused", procname)
                sleep = 30
            else:
                sleep = 0
                logging.debug("%s updating ttl: %s", procname, ttl)
                ttl = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=20)
                user.last_active = datetime.datetime.now(datetime.timezone.utc)
                user.status = "active"
                await user.save()
                
                # note details from the last loop for comparison
                last_trackid = trackid
                last_position = position
                # last_track = track
                # last_trackname = trackname

                # pull details from the current item
                trackid = currently.item.id
                trackname, _ = await trackinfo(spotify, trackid, return_track=True)
                position = currently.progress_ms/currently.item.duration_ms
                
                # pull details for the next track in the queue
                nextup_tid, nextup_name = await getnext()
                if nextup_tid is not None:
                    _, nextup_track = await trackinfo(spotify, nextup_tid, return_track=True)
                
                if trackid not in localhistory:
                    localhistory.append(trackid)
                
                remaining_ms = currently.item.duration_ms - currently.progress_ms
                seconds = int(remaining_ms / 1000) % 60
                minutes = int(remaining_ms / (1000*60)) % 60

                if remaining_ms > 30000:
                    # detect track changes
                    if trackid != last_trackid:
                        logging.info("%s detected track change at %2.0d",
                                    procname, last_position)
                        
                        # remove skipped tracks from queue
                        if last_trackid == nextup_tid:
                            logging.warning("%s removing skipped track from radio queue: %s",
                                        procname, last_trackid)
                            try:
                                await UpcomingQueue.filter(trackid=nextup_tid).delete()
                            except Exception as e:
                                logging.error("%s exception removing track from queue\n%s",
                                            procname, e)
                        
                        # rate skipped tracks based on last position
                        if last_position < 0.33:
                            value = -2
                            logging.info("%s early skip rating, %s %s %s",
                                        userid, last_trackid, value, procname)
                            await rate(userid, last_trackid, value)
                        elif last_position < 0.7:
                            value = -1
                            logging.info("%s late skip rating, %s %s %s",
                                        userid, last_trackid, value, procname)
                            await rate(userid, last_trackid, value)
                
                    if (remaining_ms - 30000) < 30000: # sleep for a few seconds
                        sleep = (remaining_ms - 30000) / 1000 # about to hit the autorate window
                    else: # sleep for thirty seconds
                        sleep = 30

                elif remaining_ms <= 30000:
                    # welcome to the end zone
                    logging.info("%s endzone %s - next up %s",
                                procname, trackname, nextup_name)
                    
                    # we got to the end of the track, so autorate +1 
                    value = 1
                    logging.info("%s setting a rating, %s %s %s", userid, trackid, value, procname)
                    await rate(userid, trackid, value, autorate=True)
                    
                    # record in the playhistory table
                    logging.debug("%s recording play history %s",
                                    procname, trackname)
                    await record(userid, trackid)

                    # if we're finishing the Currently Playing queued track
                    # we must be first to the endzone, remove track from dbqueue
                    if trackid == nextup_tid:
                        logging.info("%s removing track from radio queue: %s",
                                    procname, nextup_name)
                        try:
                            await UpcomingQueue.filter(trackid=nextup_tid).delete()
                        except Exception as e:
                            logging.error("%s exception removing track from upcomingqueue\n%s",
                                          procname, e)
                        
                        # get the next queued track
                        nextup_tid, nextup_name = await getnext()
                        nextup_name, nextup_track = await trackinfo(spotify, 
                                                                    nextup_tid, 
                                                                    return_track=True)

                    if nextup_tid in playbackqueueids:
                        # this next track is already in the queue (or context, annoyingly)
                        logging.info("%s next track already queued, don't requeue",
                                    procname)
                        
                    elif nextup_tid == trackid:
                        logging.info("%s next track currently playing, don't requeue",
                                    procname)
                    
                    else:
                        # queue up the next track for this user
                        logging.info("%s sending to spotify queue %s",
                                     procname, nextup_name)
                        try:
                            _ = await spotify.playback_queue_add(nextup_track.trackuri)
                        except Exception as e: 
                            logging.error(
                                "%s exception spotify.playback_queue_add track.uri=%s\n%s",
                                procname, nextup_name, e)

                    # sleep until this track is done
                    sleep = (remaining_ms /1000) + 2

                status = f"{trackname} {position:.0%} {minutes}:{seconds:0>2} remaining"

        if status == "not playing":
            logging.debug("%s sleeping %0.2ds - %s", procname, sleep, status)
        else:
            logging.info("%s sleeping %0.2ds - %s", procname, sleep, status)
        
        await asyncio.sleep(sleep)

    # ttl expired, clean up before exit
    logging.info("%s timed out, cleaning house", procname)
    user.watcherid = ""
    user.status = "inactive"
    await user.save()
    
    logging.info("%s exiting", procname)
    return procname




async def main():
    """kick it"""
    
    logging.info("main connecting to db")

    logging.info("main starting web_ui on port: %s", os.environ['PORT'])
    web_ui = app.run_task('0.0.0.0', os.environ['PORT'])
    taskset.add(web_ui)
    
    # spotify_watchers, queue_manager, and user_reaper
    # are added in the before_serving function

    try:
        await asyncio.gather(*taskset)
    except Exception as e:
        logging.error("main gather exception %s", e)

    await asyncio.gather(*asyncio.all_tasks())
    logging.info("main done")


if __name__ == "__main__":
    secret=os.environ['SPOTIFY_CLIENT_SECRET']
    logging.info("SPOTIFY_CLIENT_ID=%s", os.environ['SPOTIFY_CLIENT_ID'])
    logging.info("SPOTIFY_CLIENT_SECRET=%s...%s", secret[-2:], secret[:2])
    logging.info("SPOTIFY_REDIRECT_URI=%s", os.environ['SPOTIFY_REDIRECT_URI'])

    asyncio.run(main())
