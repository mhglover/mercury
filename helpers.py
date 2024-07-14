# helpers.py
"""assistive functions"""

def truncate_middle(s, n=30):
    """shorten long names"""
    if len(s) <= n:
        # string is already short-enough
        return s
    if n <= 3:
        return s[:n] # Just return the first n characters if n is very small
     # half of the size, minus the 3 .'s
    n_2 = (n - 3) // 2
    # whatever's left
    n_1 = n - n_2 - 3
    return '{0}...{1}'.format(s[:n_1], s[-n_2:]) 


def feelabout(value: int = 0) -> str:
    """Return a text string based on value."""
    if not value:
        value = 0

    if -4 <= value <= -2:
        feeling = "hate"
    elif value == -1:
        feeling = "dislike"
    elif value == 0:
        feeling = "shrug"
    elif value == 1:
        feeling = "like"
    elif 2 <= value <= 4:
        feeling = "love"
    else:
        feeling = "unrated"
    
    return feeling

