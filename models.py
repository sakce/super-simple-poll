import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, create_engine, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, scoped_session, sessionmaker

# Create SQLAlchemy engine for DuckDB with MotherDuck
connection_string = "duckdb:///md:dev_poll"
engine = create_engine(connection_string, echo=True)
SessionFactory = sessionmaker(bind=engine)
Session = scoped_session(SessionFactory)
Base = declarative_base()


# Model definitions
class Poll(Base):
    __tablename__ = "polls"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    question = Column(String, nullable=False)
    creator_id = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.now)
    allow_multiple_votes = Column(Boolean, default=False)
    hide_votes = Column(Boolean, default=False)
    hide_vote_count = Column(Boolean, default=False)  # Now controls only option vote counts
    deadline = Column(DateTime, nullable=True)
    closed = Column(Boolean, default=False)
    channel_id = Column(String, nullable=True)
    message_ts = Column(String, nullable=True)

    # Relationships
    options = relationship(
        "PollOption", back_populates="poll", cascade="all, delete-orphan"
    )

    def __init__(self, question, creator_id, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.question = question
        self.creator_id = creator_id
        self.created_at = kwargs.get("created_at", datetime.now())
        self.allow_multiple_votes = kwargs.get("allow_multiple_votes", False)
        self.hide_votes = kwargs.get("hide_votes", False)
        self.hide_vote_count = kwargs.get("hide_vote_count", False)
        # Note: we no longer need this constraint since hide_vote_count has a different meaning now
        # if self.hide_votes is False and self.hide_vote_count is True:
        #     self.hide_vote_count = False
        self.deadline = kwargs.get("deadline")
        self.closed = kwargs.get("closed", False)
        self.channel_id = (
            str(kwargs.get("channel_id")) if kwargs.get("channel_id") else None
        )
        self.message_ts = kwargs.get("message_ts")

    def add_option(self, text):
        """Add a new option to this poll"""
        option = PollOption(text=text, poll=self)
        session = Session()
        session.add(option)
        session.commit()
        return option

    def get_votes_for_option(self, option_id):
        """Get all votes for a specific option"""
        session = Session()
        votes = (
            session.query(Vote)
            .join(PollOption)
            .filter(PollOption.poll_id == self.id, Vote.option_id == option_id)
            .all()
        )
        return votes
        
    def get_total_participants(self):
        """Get the count of unique users who voted in this poll"""
        session = Session()
        return session.query(func.count(func.distinct(Vote.user_id)))\
            .join(PollOption)\
            .filter(PollOption.poll_id == self.id)\
            .scalar() or 0

    @classmethod
    def get_expired_polls(cls):
        """Get all polls that have passed their deadline but are not closed yet"""
        session = Session()
        now = datetime.now()
        return session.query(cls).filter(cls.deadline < now, cls.closed == False).all()


class PollOption(Base):
    __tablename__ = "poll_options"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    poll_id = Column(String, ForeignKey("polls.id"))
    text = Column(String, nullable=False)

    # Relationships
    poll = relationship("Poll", back_populates="options")
    votes = relationship("Vote", back_populates="option", cascade="all, delete-orphan")

    def __init__(self, text, poll=None, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.text = text
        if poll:
            self.poll = poll
            self.poll_id = poll.id
        else:
            self.poll_id = kwargs.get("poll_id")


class Vote(Base):
    __tablename__ = "votes"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, nullable=False)
    user_name = Column(String, nullable=False)
    option_id = Column(String, ForeignKey("poll_options.id"))
    timestamp = Column(DateTime, default=datetime.now)

    # Relationships
    option = relationship("PollOption", back_populates="votes")

    def __init__(self, user_id, user_name, option_id, **kwargs):
        self.id = kwargs.get("id", str(uuid.uuid4()))
        self.user_id = user_id
        self.user_name = user_name
        self.option_id = option_id
        self.timestamp = kwargs.get("timestamp", datetime.now())


# Create tables
def init_db():
    Base.metadata.create_all(engine)


# Initialize database on module import
init_db()


# Helper functions for poll management
def save_poll(poll):
    """Save a poll to the database"""
    session = Session()
    session.add(poll)
    session.commit()
    return poll


def get_poll_by_id(poll_id):
    """Retrieve a poll by its ID"""
    session = Session()
    return session.query(Poll).filter(Poll.id == poll_id).first()


def get_expired_polls():
    """Get all polls that have passed their deadline but are not closed yet"""
    return Poll.get_expired_polls()


def delete_poll(poll_id):
    """Delete a poll and all associated options and votes"""
    session = Session()
    poll = session.query(Poll).filter(Poll.id == poll_id).first()
    if poll:
        session.delete(poll)
        session.commit()
        return True
    return False


def save_vote(poll, vote_data):
    """Add a vote to a poll"""
    session = Session()
    vote = Vote(
        user_id=vote_data["user_id"],
        user_name=vote_data["user_name"],
        option_id=vote_data["option_id"],
    )
    session.add(vote)
    session.commit()
    return vote


def delete_vote(vote_id):
    """Remove a vote"""
    session = Session()
    vote = session.query(Vote).filter(Vote.id == vote_id).first()
    if vote:
        session.delete(vote)
        session.commit()
        return True
    return False
