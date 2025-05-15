import uuid
from datetime import datetime

class PollOption:
    def __init__(self, text):
        self.id = str(uuid.uuid4())
        self.text = text

class Vote:
    def __init__(self, user_id, user_name, option_id):
        self.id = str(uuid.uuid4())
        self.user_id = user_id
        self.user_name = user_name
        self.option_id = option_id
        self.timestamp = datetime.now()

class Poll:
    # In-memory storage for all polls
    polls = {}
    
    def __init__(self, question, creator_id, allow_multiple_votes=False, hide_votes=False, deadline=None, channel_id=None):
        self.id = str(uuid.uuid4())
        self.question = question
        self.creator_id = creator_id
        self.created_at = datetime.now()
        self.allow_multiple_votes = allow_multiple_votes
        self.hide_votes = hide_votes
        self.deadline = deadline
        self.closed = False
        # Ensure channel_id is always a string or None
        self.channel_id = str(channel_id) if channel_id is not None else None
        self.message_ts = None
        self.options = []
        self.votes = []
    
    def add_option(self, text):
        option = PollOption(text)
        self.options.append(option)
        return option
    
    def add_vote(self, vote):
        self.votes.append(vote)
    
    def remove_vote(self, vote_id):
        self.votes = [v for v in self.votes if v.id != vote_id]
    
    def get_votes_for_option(self, option_id):
        return [vote for vote in self.votes if vote.option_id == option_id]

# Helper functions for poll management
def save_poll(poll):
    Poll.polls[poll.id] = poll
    return poll

def get_poll_by_id(poll_id):
    return Poll.polls.get(poll_id)

def delete_poll(poll_id):
    if poll_id in Poll.polls:
        del Poll.polls[poll_id]
        return True
    return False

def save_vote(poll, vote):
    poll.add_vote(vote)
    return vote

def delete_vote(poll, vote):
    poll.remove_vote(vote.id)
    return True
