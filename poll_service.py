import logging
from datetime import datetime

from models import Poll, Vote, delete_vote, get_poll_by_id, save_poll, save_vote

logger = logging.getLogger(__name__)


class PollService:
    @staticmethod
    def create_poll(
        question,
        creator_id,
        options,
        allow_multiple_votes=False,
        hide_votes=False,
        deadline=None,
        channel_id=None,
    ):
        """
        Create a new poll

        Args:
            question (str): The poll question
            creator_id (str): Slack ID of the poll creator
            options (list): List of option text strings
            allow_multiple_votes (bool): Whether to allow users to vote for multiple options
            hide_votes (bool): Whether to hide votes until the poll is closed
            deadline (datetime, optional): When the poll should automatically close
            channel_id (str): The Slack channel ID where the poll was created

        Returns:
            Poll: The created poll object
        """
        poll = Poll(
            question=question,
            creator_id=creator_id,
            allow_multiple_votes=allow_multiple_votes,
            hide_votes=hide_votes,
            deadline=deadline,
            channel_id=channel_id,
        )

        # Add options
        for option_text in options:
            poll.add_option(option_text)

        save_poll(poll)
        logger.info(f"Created poll {poll.id} with {len(options)} options")
        return poll

    @staticmethod
    def add_vote(poll_id, user_id, user_name, option_id):
        """
        Add a vote to a poll

        Args:
            poll_id (str): The poll ID
            user_id (str): The Slack user ID
            user_name (str): The Slack username
            option_id (str): The option ID to vote for

        Returns:
            tuple: (success, message)
        """
        poll = get_poll_by_id(poll_id)
        if not poll:
            return False, "Poll not found"

        if poll.closed:
            return False, "Poll is closed"

        # Check if option exists
        if not any(option.id == option_id for option in poll.options):
            return False, "Option not found"

        # Check if user already voted
        user_votes = [vote for vote in poll.votes if vote.user_id == user_id]

        # If multiple votes aren't allowed and user already voted
        if not poll.allow_multiple_votes and user_votes:
            # If voting for same option, remove the vote (toggle)
            if any(vote.option_id == option_id for vote in user_votes):
                for vote in user_votes[:]:
                    if vote.option_id == option_id:
                        delete_vote(poll, vote)
                return True, "Vote removed"
            else:
                # If voting for a different option, remove old votes
                for vote in user_votes[:]:
                    delete_vote(poll, vote)
        else:
            # Check if user already voted for this specific option
            existing_vote = next(
                (vote for vote in user_votes if vote.option_id == option_id), None
            )

            if existing_vote:
                # Remove the vote (toggle behavior)
                delete_vote(poll, existing_vote)
                return True, "Vote removed"

        # Add new vote
        vote = Vote(user_id=user_id, user_name=user_name, option_id=option_id)
        save_vote(poll, vote)
        return True, "Vote added"

    @staticmethod
    def close_poll(poll_id, user_id):
        """
        Close a poll

        Args:
            poll_id (str): The poll ID
            user_id (str): The Slack user ID attempting to close the poll

        Returns:
            tuple: (success, message)
        """
        poll = get_poll_by_id(poll_id)
        if not poll:
            return False, "Poll not found"

        if poll.closed:
            return False, "Poll is already closed"

        if poll.creator_id != user_id:
            return False, "Only the poll creator can close this poll"

        poll.closed = True
        save_poll(poll)
        return True, "Poll closed successfully"

    @staticmethod
    def get_poll_results(poll_id):
        """
        Get the results of a poll

        Args:
            poll_id (str): The poll ID

        Returns:
            dict: Poll results data
        """
        poll = get_poll_by_id(poll_id)
        if not poll:
            return None

        results = {
            "question": poll.question,
            "creator_id": poll.creator_id,
            "created_at": poll.created_at.isoformat(),
            "closed": poll.closed,
            "hide_votes": poll.hide_votes,
            "options": [],
        }

        # Add results for each option
        for option in poll.options:
            votes = poll.get_votes_for_option(option.id)
            option_result = {
                "id": option.id,
                "text": option.text,
                "count": len(votes),
            }

            # Add voter information if votes aren't hidden
            if not poll.hide_votes:
                option_result["voters"] = [
                    {"id": vote.user_id, "name": vote.user_name} for vote in votes
                ]

            results["options"].append(option_result)

        # Sort options by vote count descending
        results["options"].sort(key=lambda x: x["count"], reverse=True)

        return results

    @staticmethod
    def check_expired_polls():
        """
        Check for polls that have passed their deadline and close them

        Returns:
            list: List of poll IDs that were closed
        """
        closed_polls = []
        current_time = datetime.now()

        for poll_id, poll in Poll.polls.items():
            if poll.deadline and current_time > poll.deadline and not poll.closed:
                poll.closed = True
                save_poll(poll)
                closed_polls.append(poll_id)
                logger.info(f"Automatically closed poll {poll_id} due to deadline")

        return closed_polls
