from distutils.log import error
from collections import deque
import tekore as tk
from random import choice
import asyncio
import os
from dotenv import load_dotenv
import discord
from discord import Game, Embed
from discord.ext import commands
import logging
import discord
import asyncio
from quart import Quart, request, redirect
from peewee import *
import pickle
from datetime import datetime

db = SqliteDatabase('radio.db')

app = Quart(__name__)
client = discord.Client()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
    datefmt="%Y-%m-%d %H:%M:%S"
    )

load_dotenv()  # take environment variables from .env

CHANNEL = os.environ['DISCORD_CHANNEL']
SERVER = os.environ['DISCORD_SERVER']
PREFIX=os.environ['DISCORD_COMMAND_PREFIX']
USER=os.environ['USER']
PORT=os.environ['PORT']
discord_token = os.environ['DISCORD_TOKEN']

conf = tk.config_from_environment()
cred = tk.Credentials(*conf)
token_spotify = tk.request_client_token(*conf[:2])

description = "Spotify track search bot using Tekore"
bot = commands.Bot(command_prefix=PREFIX, description=description, activity=Game(name=f"{PREFIX} help"))
spotify = tk.Spotify(token_spotify, asynchronous=True)
queue = deque()


class User(Model):
    token = BlobField(null=True)

    class Meta:
        database = db


class Rating(Model):
    userid = CharField()
    trackid = CharField()
    rating = IntegerField()

    class Meta:
        database = db


class PlayHistory(Model):
    trackid = CharField()
    played_at = TimestampField()

    class Meta:
        database = db
        indexes = (
            (('trackid', 'played_at'), True),
        )


@app.before_serving
async def before_serving():
    logging.info("before_serving")


@bot.event
async def on_ready():
    logging.info("Bot Ready!")

@bot.command()
async def track(ctx, *, query: str = None):
    if query is None:
        if ctx.author.id in users:
            user = users[ctx.author.id]
            if type(user.token) == bytes:
                token = pickle.loads(user.token)
            else:
                token = user.token

            if token.is_expiring:
                token = cred.refresh(token)
                user.token = token
                user.save()
                users[user] = token
            
            with spotify.token_as(token):
                playback = await spotify.playback_currently_playing()
                if playback is None:
                    await ctx.send("Nothing playing")
                else:
                    artist = " & ".join([x.name for x in playback.item.artists])
                    name = playback.item.name
                    await ctx.send(f"{artist} - {name}")
                return
            

        await ctx.send("No search query specified")
        return

    tracks, = await spotify.search(query, limit=5)
    embed = Embed(title="Track search results", color=0x1DB954)
    # embed.set_thumbnail(url="https://i.imgur.com/890YSn2.png")
    embed.set_footer(text="Requested by " + ctx.author.display_name)

    for t in tracks.items:
        artist = t.artists[0].name
        url = t.external_urls["spotify"]

        message = "\n".join([
            "[Spotify](" + url + ")",
            ":busts_in_silhouette: " + artist,
            ":cd: " + t.album.name
        ])
        embed.add_field(name=t.name, value=message, inline=False)

    await ctx.send(embed=embed)


@bot.command()
async def devices(ctx, *, query: str = None):
    if ctx.author.id in users:
        user = users[ctx.author.id]
        if type(user.token) == bytes:
            token = pickle.loads(user.token)
        else:
            token = user.token

        if token.is_expiring:
            token = cred.refresh(token)
            user.token = token
            user.save()
            users[user] = token
        
        with spotify.token_as(token):
            devices = await spotify.playback_devices()
            await ctx.send(f"fetched devices: {devices}")
            return

    else:
        ctx.send("Sorry, you aren't authorized.")
        return


@bot.command()
async def spotme(ctx):
    if ctx.author.id in users:
        await ctx.channel.send(f"You're already set up as user {ctx.author.id}! Starting a player!")
        bot.loop.create_task(spotify_player())
        bot.loop.create_task(queue_manager())

    else:
        # scope = tk.scope.user_read_currently_playing
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

        auths[auth.state] = auth
        users[auth.state] = ctx.author.id

        # Returns: 1
        channel = await ctx.author.create_dm()
        logging.info(f"authentication for {ctx.author.name}")
        await channel.send(f"To grant the bot access to your Spotify account, click here: {auth.url}")


@app.route('/spotify/callback', methods=['GET','POST'])
async def spotify_callback():

    code = request.args.get('code', "")
    state = request.args.get('state', "")
    auth = auths.pop(state, None)
    spotifyid = users.pop(state, None) 

    if auth is None:
        return 'Invalid state!', 400

    token = auth.request_token(code, state)
    user = User.create(id=spotifyid, token=pickle.dumps(token))
    users[spotifyid] = user
    return redirect(f"https://discord.com/channels/{SERVER}/{CHANNEL}", 307)


async def getuser(userid=USER):
    u = User.get_by_id(userid)
    token = pickle.loads(u.token)
    if token.is_expiring:
        token = cred.refresh(token)
    return token


async def trackinfo(trackid):
    track = await spotify.track(trackid)
    artist = " & ".join([x.name for x in track.artists])
    name = track.name
    return f"{artist} - {name}"


async def updatehistory():
    with spotify.token_as(token):
        history = await spotify.playback_recently_played()
        tracks = spotify.all_items(history)
        dbhistory = [(i.trackid, int(i.played_at.timestamp())) for i in PlayHistory.select()]

        async for played in tracks:
            if (played.track.id, int(played.played_at.timestamp())) not in dbhistory:
                logging.info(f"adding to history: {played.track.id}")
                try:
                    h = PlayHistory(trackid=played.track.id, played_at=int(played.played_at.timestamp()))
                    h.save()
                except Exception as e:
                    logging.exception("tried to write to history and failed with exception")


async def spotify_player():
    logging.info("starting spotify player task")
    token = await getuser()
    history = await updatehistory()
    ctx = bot.get_channel(CHANNEL)
    # await ctx.send(f"bot connecting to channel")
    
    while True:
        
        with spotify.token_as(token):
            currently = await spotify.playback_currently_playing()
        
        if currently is None:
            logging.info(f"not currently playing -- attempting to resume")
            spotify.playback_resume()
            sleep = 60
        else:
            nowplaying = await trackinfo(currently.item.id)
            remaining_ms = currently.item.duration_ms - currently.progress_ms
            logging.info(f"now playing {nowplaying}, {remaining_ms/1000}s remaining")
        
            if remaining_ms < 20000:
                logging.debug(f"remaining_ms: {remaining_ms}")
                artist = " & ".join([x.name for x in currently.item.artists])
                name = currently.item.name
                logging.info(f"rating {artist} - {name}")
                Rating.insert(userid=user.id, trackid=currently.item.id)
                if len(queue) > 0:
                    next_up = queue.popleft()
                    info = await trackinfo(next_up)
                    track = await spotify.track(next_up)
                    # await ctx.send(f"Now Playing: {info}")
                    # await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.playing, name={info}))
                    logging.info(f"queuing trackid: {next_up} [{info}]")
                    
                    with spotify.token_as(token):
                        result = await spotify.playback_queue_add(track.uri)
                        PlayHistory.insert(trackid=currently.item.id)
                    sleep = (remaining_ms / 1000) +1
                else:
                    sleep = 0.01
            else:
                sleep = (remaining_ms / 2 ) / 1000

        logging.debug(f"sleeping for {sleep} seconds")
        await asyncio.sleep(sleep)
    
    await bot.sendMessage("spotify player dying")


async def queue_manager():
    logging.info(f'starting queue manager')
    token = await getuser()
    
    while True:
        while len(queue) < 5:

            with spotify.token_as(token):
                history = [i.trackid for i in PlayHistory.select()]
                tops = [item.id async for item in spotify.all_items(await spotify.current_user_top_tracks())]
                saveds = [item.track.id async for item in spotify.all_items(await spotify.saved_tracks())]
                potentials = [x for x in tops + saveds if x not in history and x not in queue]
                trackid =  choice(potentials)
                t = await trackinfo(trackid)
                
                queue.append(trackid)

                logging.info(f"selected {t}")
                # logging.info(f"tops: {len(tops)} saveds: {len(saveds)} history: {len(history)} potentials: {len(potentials)}")


        await asyncio.sleep(60)



if __name__ == "__main__":
    db.connect()
    db.create_tables([User, Rating, PlayHistory])

    auths = {}  # Ongoing authorisations: state -> UserAuth
    # users = {}  # User tokens: state -> token (use state as a user ID)
    users = {}
    query = User.select()
    for user in query:
        token = pickle.loads(user.token)
        if token.is_expiring:
            token = cred.refresh(token)
        user.token = token
        users[user.id] = user
        
    bot.loop.create_task(app.run_task('0.0.0.0', PORT))
    bot.run(discord_token)
    
