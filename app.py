#!/usr/bin/env python
"""mercury radio"""
import datetime
import logging
import os
import asyncio
import pickle
import tekore as tk
from dotenv import load_dotenv
from humanize import naturaltime
from quart import Quart, request, redirect, render_template, session
from tortoise.contrib.quart import register_tortoise
from models import User, WebData, PlayHistory, WebUser
from watchers import user_reaper, watchman, spotify_watcher
from users import getactiveusers, getuser, getactivewebusers
from queue_manager import queue_manager, getnext
from raters import rate_history, rate_saved, get_track_ratings, rate
from raters import get_recent_playhistory_with_ratings
from spot_funcs import trackinfo, getrecents, normalizetrack, get_webtrack

# pylint: disable=broad-exception-caught
# pylint: disable=trailing-whitespace, trailing-newlines

load_dotenv()  # take environment variables from .env

app = Quart(__name__)
app.config.from_prefixed_env()
app.secret_key = os.getenv("QUART_SECRET_KEY", default="1234567890")

conf = tk.config_from_environment()
cred = tk.Credentials(*conf)

token_spotify = tk.request_client_token(*conf[:2])
spotify = tk.Spotify(token_spotify, asynchronous=True)

taskset = set()
auths = {}

# region - logging
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
#endregion

# set up the async database connection
# as a part of Quart so it's easy
register_tortoise(
    app,
    db_url=os.environ['DATABASE_URL'],
    modules={"models": ["models"]},
    generate_schemas=False,
)

# if we need them, start some
# background tasks for users and queues
@app.before_serving
async def before_serving():
    """pre"""
    procname="before_serving"

    # check to see which tasks we're supposed to be running on this instance
    run_tasks = os.getenv('RUN_TASKS', 'spotify_watcher queue_manager web_ui')
    logging.debug("before_serving running tasks: %s", run_tasks)
    
    if "queue_manager" in run_tasks:
        logging.debug("%s creating a queue manager task", procname)
        qm = asyncio.create_task(queue_manager(spotify),name="queue_manager")
        taskset.add(qm)
        qm.add_done_callback(taskset.remove(qm))

    if "spotify_watcher" in run_tasks:
        
        if os.getenv("NOOVERTAKE") is not True:
            logging.info("%s overtaking any existing watcher tasks", 
                        procname)    
            await User.select_for_update().exclude(watcherid='').update(watcherid='')

        logging.debug("%s launching a user_reaper task", procname)
        reaper_task = asyncio.create_task(user_reaper(), name="user_reaper")
        taskset.add(reaper_task)
        reaper_task.add_done_callback(taskset.remove(reaper_task))
        
        logging.debug("%s pulling active users for spotify watchers", procname)
        active_users = await getactiveusers()
        for user in active_users:
            await watchman(taskset, cred, spotify, spotify_watcher, user)
        
        nextup = await getnext(get_all=True)
        for track in nextup:
            logging.info("upcoming recommendation: [%s] %s (%s)", 
                         track.reason, track.trackname, naturaltime(track.expires_at))
        
        logging.debug("%s ready", procname)


@app.before_request
def before_request():
    """save cookies even if you close your browser"""
    session.permanent = True


@app.route('/', methods=['GET'])
async def index():
    """show the now playing page"""
    
    # check whether this is a known user or they need to login
    user_id = session.get('user_id', None)
    
    nextup = await getnext(webtrack=True)
    
    # what's happening y'all
    web_data = WebData(
        nextup=nextup,
        ratings=[]
        )
    
    # go ahead and return if this isn't an active user
    if user_id is None or user_id == '':
        web_data.history = await get_recent_playhistory_with_ratings(2)
        # get outta here kid ya bother me
        return await render_template('index.html', w=web_data.to_dict())
    
    # okay, we got a live one - get user details
    web_data.user, token = await getuser(cred, user_id)
    web_data.nextup = await getnext(webtrack=True, user=web_data.user)
    
    if web_data.nextup:
        web_data.users = await getactivewebusers(web_data.nextup.track_id)
    
    web_data.history = await get_recent_playhistory_with_ratings(web_data.user.id)
    
    # what's the player's current status?
    with spotify.token_as(token):
        currently = await spotify.playback_currently_playing()

    # set some return values
    if currently is not None:
        web_data.refresh = ((currently.item.duration_ms - currently.progress_ms) // 1000) +1
        track = await trackinfo(spotify, currently.item.id)
        web_data.track = await get_webtrack(track, web_data.user)
        web_data.users = await getactivewebusers(track)
        
        # web_data.rating = await get_current_rating(
            # web_data.track, activeusers=web_data.activeusers)

        # see if we need to launch a task for this user
        await watchman(taskset, cred, spotify, spotify_watcher, web_data.user)
    
    
    # let's see it then
    return await render_template('index.html', w=web_data.to_dict())


@app.route('/logout', methods=['GET'])
async def logout():
    """set a user as inactive and forget cookies"""
    
    # check for the session credentials and unset them
    user_id = session.get('user_id', None)
    session['user_id'] = ""
    
    if user_id:
        user = await User.get(id=user_id)
        user.status = "inactive"
        await user.save()
    
    return redirect("/")


@app.route('/auth', methods=['GET'])
async def spotify_authorization():
    """explain what's going on and redirect to spotify for authorization"""
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
    
    nextup=await getnext(webtrack=True)
    w = WebData()
        
    return await render_template('auth.html',
                                 w=w,
                                 trackname=nextup.trackname,
                                 spoturl=auth.url)


@app.route('/spotify/callback', methods=['GET','POST'])
async def spotify_callback():
    """create a user record and set up initial ratings"""
    procname = "spotify_callback"

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

    p = pickle.dumps(token)

    logging.info("%s get_or_create user record for %s", procname, spotifyid)
    n = datetime.datetime.now(datetime.timezone.utc)
    user, created = await User.get_or_create(spotifyid=spotifyid,
                                             defaults={
                                                 "token": p,
                                                 "last_active": n,
                                                 "displayname": spotify_user.display_name,
                                                 "status": "active"
                                             })
    if created is False:
        logging.info('%s found user %s', procname, spotifyid)
        user.last_active = datetime.datetime.now
        user.status = "active"
        await user.save()
    else:
        logging.info("%s creating new user %s", procname, spotifyid)
        await user.save()
        
        logging.info("%s pulling ratings to populate user", procname)
        asyncio.create_task(pullratings(user.id), name=f"pullratings_{spotifyid}")
    
    session['user_id'] = user.id
    logging.info("%s redirecting %s back to /", procname, spotifyid)
    return redirect("/")


@app.route('/dash', methods=['GET'])
async def dashboard():
    """show what's happening"""
    
    user_id = session.get('user_id', None)
    if not user_id:
        return redirect("/")
    
    user, _ = await getuser(cred, user_id)
    
    if user.role != "admin":
        return redirect("/")

    # what's happening y'all
    data = WebData(
        history=await getrecents(),
        users = await getactiveusers(),
        nextup = await getnext(webtrack=True, user=user),
        ratings=[]
        )

    # ratings = [x for x in Rating.select()]

    # queued = [await trackinfo(trackid) for trackid in queue]
    # try:
    #     dbqueue = await Recommendation.all().values_list('trackid', flat=True)
    # except Exception as e: # pylint: disable=W0718
    #     logging.error("dashboard database queue retrieval exception: %s", e)

    # tracknames = await Track.filter(trackid__in=dbqueue).values_list('trackname', flat=True)

    # tasknames = [x.get_name() for x in asyncio.all_tasks() if "Task-" not in x.get_name()]
    
    return await render_template('dashboard.html', w=data)


@app.route('/pullratings', methods=['GET'])
async def pullratings(user_id=None):
    """load up a bunch of ratings for a user"""
    
    if not user_id:
        user_id = session.get('user_id', None)
    
    if not user_id:
        return redirect("/")
    
    user, token = await getuser(cred, user_id)

    with spotify.token_as(token):

        # rate recent history (20 items)
        await rate_history(spotify, user, token)
        
        # rate saved tracks
        await rate_saved(spotify, user, token)
        
        # tops = await spotify.all_items(await spotify.current_user_top_tracks())

        return redirect("/")


@app.route('/user/<target_id>', methods=['GET', 'POST'])
async def web_user(target_id):
    """allow user profile updates"""
    procname = "web_user"
    
    user_id = session.get('user_id', None)
    
    if not user_id:
        logging.info("%s no user_id in session", procname)
        return redirect("/", 303)
    
    user, _ = await getuser(cred, user_id)
    target = await User.get_or_none(id=target_id)
    
    if not target:
        return redirect("/")
    
    u = WebUser(
        displayname=target.displayname,
        user_id=target.id
    )
    w = WebData(user=user)
    
    if request.method == "GET":
        return await render_template("user.html", w=w, u=u)
    
    if request.method == "POST":
        logging.info("%s updating user record", procname)
        logging.info("%s fetching user %s", 
                     procname, user.id)
        
        form = await request.form
        displayname = form['displayname']
        logging.info("%s updating displayname: %s",
                     procname, displayname)
        if displayname is not None:
            logging.info("%s updating displayname: %s",
                         procname, displayname)
            user.displayname = displayname
            await user.save()
        
        return redirect(request.referrer)
    
    return redirect("/")


@app.route('/user/impersonate/<target_id>', methods=['GET'])
async def user_impersonate(target_id):
    """act as somebody else"""
    procname = "user_impersonate"
    
    user_id = session.get('user_id', None)
    if not user_id:
        return redirect("/")
    
    user, _ = await getuser(cred, user_id)
    
    if "admin" not in user.role:
        return redirect("/")
    
    logging.warning("%s admin user %s impersonation: %s", procname, target_id)
    session['user_id'] = target_id
    
    return redirect("/")


@app.route('/follow/<target_id>')
async def follow(target_id):
    """listen with a friend"""
    procname = "follow"
    user_id = session.get('user_id', None)
    if not user_id:
        return redirect(request.referrer)
    
    user, _ = await getuser(cred, user_id)
    
    if session['user_id'] == target_id:
        logging.warning("%s user %s tried to follow itself", procname, user_id)
        return redirect(request.referrer)

    user.status = "following"
    user.watcherid = target_id
    await user.save()
    
    logging.info("%s updated user %s - status: %s - watcherid: %s", 
                         procname, user_id, user.status, user.watcherid)
    return redirect(request.referrer)


@app.route('/track/<track_id>')
async def web_track(track_id):
    """display/edit track details"""
    
    # get the details we need to show
    user_id = session.get('user_id', None)
    if not user_id:
        return redirect("/")
    
    user, _ = await getuser(cred, user_id)
    
    track = await normalizetrack(track_id)
    webtrack = await get_webtrack(track, user=user)
    
    # nextup = await getnext(webtrack=True, user=user)
    
    ratings = await get_track_ratings(track)
    
    # what's happening y'all
    w = WebData(
        user = user,
        track = webtrack,
        ratings = ratings,
        # nextup = nextup,
        refresh = 0
        )
    
    ph = await PlayHistory.filter(track_id=track.id).order_by('-played_at').prefetch_related("user")
    history = [f"{naturaltime(x.played_at)} by {x.user.displayname}" for x in ph] 
    
    return await render_template('track.html', history=history, w=w.to_dict())


@app.route('/track/<track_id>/rate/<value>')
async def rate_track(track_id, value):
    """set a rating for a user/track"""
    user_id = session.get('user_id', None)
    if not user_id:
        return redirect("/")
    
    user, _ = await getuser(cred, user_id)
    track = await normalizetrack(track_id)
    
    if int(value) > 4 or int(value) < -4:
        return redirect("/")
    
    logging.info("writing a rating for %s - %s - %s", user.displayname, value, track.trackname)
    
    await rate(user, track, value, downrate=True)
    
    # return redirect("/track/" + track_id)
    return redirect(request.referrer)


async def main():
    """kick it"""
    secret=os.environ['SPOTIFY_CLIENT_SECRET']
    logging.debug("SPOTIFY_CLIENT_ID=%s", os.environ['SPOTIFY_CLIENT_ID'])
    logging.debug("SPOTIFY_CLIENT_SECRET=%s...%s", secret[-2:], secret[:2])
    logging.debug("SPOTIFY_REDIRECT_URI=%s", os.environ['SPOTIFY_REDIRECT_URI'])
    logging.info("main starting web_ui on port: %s", os.environ['PORT'])
    web_ui = app.run_task('0.0.0.0', os.environ['PORT'])
    taskset.add(web_ui)

    try:
        await asyncio.gather(*taskset)
    except Exception as e:
        logging.error("main gather exception %s", e)

    await asyncio.gather(*asyncio.all_tasks())
    logging.info("main done")


if __name__ == "__main__":
    asyncio.run(main())
