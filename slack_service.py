import os
import logging
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

class SlackService:
    def __init__(self):
        self.token = os.environ.get("SLACK_BOT_TOKEN")
        if not self.token:
            logger.error("SLACK_BOT_TOKEN environment variable is not set")
            raise ValueError("SLACK_BOT_TOKEN environment variable is not set")
        
        self.client = WebClient(token=self.token)
    
    def post_message(self, channel, text=None, blocks=None):
        """
        Post a message to a Slack channel
        
        Args:
            channel (str): Channel ID to post message to
            text (str, optional): Text of the message
            blocks (list, optional): Blocks for the message
        
        Returns:
            dict: Response from Slack API
        """
        try:
            return self.client.chat_postMessage(
                channel=channel,
                text=text,
                blocks=blocks
            )
        except SlackApiError as e:
            logger.error(f"Error posting message: {e}")
            raise
    
    def update_message(self, channel, ts, text=None, blocks=None):
        """
        Update an existing Slack message
        
        Args:
            channel (str): Channel ID containing the message
            ts (str): Timestamp of the message to update
            text (str, optional): New text for the message
            blocks (list, optional): New blocks for the message
        
        Returns:
            dict: Response from Slack API
        """
        try:
            return self.client.chat_update(
                channel=channel,
                ts=ts,
                text=text,
                blocks=blocks
            )
        except SlackApiError as e:
            logger.error(f"Error updating message: {e}")
            raise
    
    def post_ephemeral(self, channel, user, text=None, blocks=None):
        """
        Post an ephemeral message visible only to a specific user
        
        Args:
            channel (str): Channel ID to post message to
            user (str): User ID who will see the message
            text (str, optional): Text of the message
            blocks (list, optional): Blocks for the message
        
        Returns:
            dict: Response from Slack API
        """
        try:
            return self.client.chat_postEphemeral(
                channel=channel,
                user=user,
                text=text,
                blocks=blocks
            )
        except SlackApiError as e:
            logger.error(f"Error posting ephemeral message: {e}")
            raise
    
    def get_user_info(self, user_id):
        """
        Get information about a Slack user
        
        Args:
            user_id (str): ID of the user
        
        Returns:
            dict: User information from Slack API
        """
        try:
            return self.client.users_info(user=user_id)
        except SlackApiError as e:
            logger.error(f"Error getting user info: {e}")
            raise
