"""functions for user manipulations"""
import logging
import pickle
from models import User, Rating, WebUser, Track

# pylint: disable=broad-exception-caught
# pylint: disable=trailing-whitespace

async def getuser(cred, user):
    """fetch user details
    cred - spotify credentials object
    userid - either a user object, a user.id, or a user.spotifyid
    
    returns: user object, spotify token
    """
    if isinstance(user, str):
        try:
            user = await User.get(spotifyid=user)
        except Exception as e:
            logging.error("getuser exception 1 fetching user\n%s", e)
    
    elif isinstance(user, int):
        try:
            user = await User.get(id=user)
        except Exception as e:
            logging.error("getuser exception 2 fetching user\n%s", e)
    
    if not isinstance(user, User):
        logging.error("getuser unable to find user %s", user)
        user = None
        token = None
    else:
        token = pickle.loads(user.token)
        if token.is_expiring:
            try:
                token = cred.refresh(token)
            except Exception as e:
                logging.error("getuser exception refreshing token\n%s", e)
            user.token = pickle.dumps(token)
            await user.save()

    return user, token


async def getactiveusers():
    """fetch details for the active users
    
    returns: list of Users
    """
    return await User.exclude(status="inactive")


async def getactivewebusers(trackid):
    """fetch
    
    returns: list of Webusers   
    """
    webusers = []
    activeusers = await getactiveusers()
    track = await Track.filter(id=trackid).get().prefetch_related("ratings")
    user_ratings = {x.user_id:x.rating for x in track.ratings}
    for user in activeusers:
        if user.id in user_ratings:
            webusers.append(WebUser(displayname=user.displayname,
                    user_id=user.id,
                    color=colorize(user_ratings[user.id]),
                    rating=user_ratings[user.id],
                    track_id=trackid,
                    trackname=track.trackname))
    
    return webusers


def colorize(value: int):
    """return a text string based on value"""
    if value is None:
        return 'unrated'
    if value <= -2: 
        return 'hate'
    elif value == -1:
        return 'dislike'
    elif value == 0:
        return 'shrug'
    elif value == 1:
        return 'like'
    elif value > 1:
        return 'love'
