import os
import json
import logging
from datetime import datetime, timedelta
import threading
import time

from flask import Flask, request, jsonify, render_template
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from slack_sdk.errors import SlackApiError

from models import Poll, PollOption, Vote, get_poll_by_id, save_poll, save_vote, delete_vote

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET")

# Initialize Slack app with bot token
slack_app = App(
    token=os.environ.get("SLACK_BOT_TOKEN"),
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET")
)
handler = SlackRequestHandler(slack_app)

# Background thread to check for expired polls
def check_expired_polls():
    while True:
        try:
            current_time = datetime.now()
            all_polls = [poll for poll_id, poll in Poll.polls.items() if poll.deadline and not poll.closed]
            
            for poll in all_polls:
                if poll.deadline and current_time > poll.deadline and not poll.closed:
                    logger.info(f"Automatically closing poll {poll.id} due to deadline")
                    poll.closed = True
                    save_poll(poll)
                    
                    # Update the message to reflect that the poll is closed
                    try:
                        slack_app.client.chat_update(
                            channel=poll.channel_id,
                            ts=poll.message_ts,
                            blocks=generate_poll_blocks(poll)
                        )
                    except SlackApiError as e:
                        logger.error(f"Error updating poll message: {e}")
        except Exception as e:
            logger.error(f"Error in expired polls check: {e}")
        
        # Check every minute
        time.sleep(60)

# Start the background thread
poll_checker_thread = threading.Thread(target=check_expired_polls, daemon=True)
poll_checker_thread.start()

# Helper function to generate poll blocks for Slack messages
def generate_poll_blocks(poll):
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📊 {poll.question}"
            }
        },
        {
            "type": "divider"
        }
    ]
    
    # If poll is closed, show a notice
    if poll.closed:
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "*This poll is closed*"
                }
            ]
        })
    
    # Add deadline if it exists
    if poll.deadline:
        deadline_str = poll.deadline.strftime("%Y-%m-%d %H:%M:%S")
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"*Deadline:* {deadline_str}"
                }
            ]
        })
    
    # Poll options section
    for option in poll.options:
        # Count votes for this option
        vote_count = sum(1 for vote in poll.votes if vote.option_id == option.id)
        
        # Show voters if not hidden
        voters_text = ""
        if not poll.hide_votes and vote_count > 0:
            voters = [vote.user_name for vote in poll.votes if vote.option_id == option.id]
            voters_text = f" - Votes: {', '.join(voters)}"
        
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{option.text}* - {vote_count} vote(s){voters_text}"
            },
            "accessory": {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "Vote"
                },
                "value": f"{poll.id}|{option.id}",
                "action_id": "vote_button",
                "style": "primary"
            } if not poll.closed else None
        })
    
    # Add controls for poll creator
    if not poll.closed:
        blocks.append({
            "type": "divider"
        })
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Close Poll"
                    },
                    "value": poll.id,
                    "action_id": "close_poll",
                    "style": "danger"
                }
            ]
        })
    else:
        # If poll is closed, add show results button
        blocks.append({
            "type": "divider"
        })
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Show Results"
                    },
                    "value": poll.id,
                    "action_id": "show_results"
                }
            ]
        })
    
    return blocks

# Function to generate results blocks
def generate_results_blocks(poll):
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📊 Results: {poll.question}"
            }
        },
        {
            "type": "divider"
        }
    ]
    
    # Group votes by option
    vote_counts = {}
    for option in poll.options:
        vote_counts[option.id] = 0
    
    for vote in poll.votes:
        vote_counts[vote.option_id] += 1
    
    # Sort options by vote count
    sorted_options = sorted(
        [(option, vote_counts[option.id]) for option in poll.options],
        key=lambda x: x[1],
        reverse=True
    )
    
    # Add each option with vote count
    for option, count in sorted_options:
        voters_text = ""
        if not poll.hide_votes and count > 0:
            voters = [vote.user_name for vote in poll.votes if vote.option_id == option.id]
            voters_text = f"\nVoters: {', '.join(voters)}"
            
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{option.text}*: {count} vote(s){voters_text}"
            }
        })
    
    return blocks

# Slash command handler for creating a poll
@slack_app.command("/poll")
def create_poll(ack, body, client):
    # Acknowledge the command request
    ack()
    
    # Get user information
    user_id = body["user_id"]
    channel_id = body["channel_id"]
    
    # Open a modal for poll creation
    try:
        client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "poll_creation_modal",
                "title": {
                    "type": "plain_text",
                    "text": "Create a Poll"
                },
                "submit": {
                    "type": "plain_text",
                    "text": "Create"
                },
                "close": {
                    "type": "plain_text",
                    "text": "Cancel"
                },
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "question_block",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "question",
                            "placeholder": {
                                "type": "plain_text",
                                "text": "What would you like to know?"
                            }
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Poll Question"
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "options_block",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "options",
                            "multiline": True,
                            "placeholder": {
                                "type": "plain_text",
                                "text": "Enter one option per line"
                            }
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Poll Options"
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "deadline_block",
                        "optional": True,
                        "element": {
                            "type": "datepicker",
                            "action_id": "deadline_date"
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Deadline Date (Optional)"
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "deadline_time_block",
                        "optional": True,
                        "element": {
                            "type": "timepicker",
                            "action_id": "deadline_time"
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Deadline Time (Optional)"
                        }
                    },
                    {
                        "type": "input",
                        "block_id": "settings_block",
                        "optional": True,
                        "element": {
                            "type": "checkboxes",
                            "action_id": "settings",
                            "options": [
                                {
                                    "text": {
                                        "type": "plain_text",
                                        "text": "Allow multiple votes per user"
                                    },
                                    "value": "multiple_votes"
                                },
                                {
                                    "text": {
                                        "type": "plain_text",
                                        "text": "Hide votes until poll is closed"
                                    },
                                    "value": "hide_votes"
                                }
                            ]
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Poll Settings"
                        }
                    }
                ],
                "private_metadata": channel_id
            }
        )
    except SlackApiError as e:
        logger.error(f"Error opening modal: {e}")

# Modal submission handler
@slack_app.view("poll_creation_modal")
def handle_poll_submission(ack, body, client, view):
    # Acknowledge the view submission
    ack()
    
    # Extract values from the modal
    question = view["state"]["values"]["question_block"]["question"]["value"]
    options_text = view["state"]["values"]["options_block"]["options"]["value"]
    
    # Parse deadline if provided
    deadline = None
    try:
        if (view["state"]["values"]["deadline_block"]["deadline_date"]["selected_date"] and
            view["state"]["values"]["deadline_time_block"]["deadline_time"]["selected_time"]):
            
            deadline_date = view["state"]["values"]["deadline_block"]["deadline_date"]["selected_date"]
            deadline_time = view["state"]["values"]["deadline_time_block"]["deadline_time"]["selected_time"]
            
            # Parse date and time and create datetime object
            deadline_str = f"{deadline_date} {deadline_time}"
            deadline = datetime.strptime(deadline_str, "%Y-%m-%d %H:%M")
    except Exception as e:
        logger.error(f"Error parsing deadline: {e}")
    
    # Parse settings
    settings = view["state"]["values"]["settings_block"]["settings"]["selected_options"]
    allow_multiple_votes = any(item["value"] == "multiple_votes" for item in settings) if settings else False
    hide_votes = any(item["value"] == "hide_votes" for item in settings) if settings else False
    
    # Split options by newline
    option_texts = [opt.strip() for opt in options_text.split("\n") if opt.strip()]
    
    # Create poll object
    poll = Poll(
        question=question,
        creator_id=body["user"]["id"],
        allow_multiple_votes=allow_multiple_votes,
        hide_votes=hide_votes,
        deadline=deadline,
        channel_id=view["private_metadata"]
    )
    
    # Add options
    for text in option_texts:
        poll.add_option(text)
    
    # Save poll
    save_poll(poll)
    
    # Post the poll to the channel
    try:
        result = client.chat_postMessage(
            channel=poll.channel_id,
            blocks=generate_poll_blocks(poll),
            text=f"Poll: {question}"  # Fallback text for notifications
        )
        
        # Store the message timestamp for later updates
        poll.message_ts = result["ts"]
        save_poll(poll)
        
    except SlackApiError as e:
        logger.error(f"Error posting poll: {e}")

# Vote button handler
@slack_app.action("vote_button")
def handle_vote(ack, body, client):
    # Acknowledge the button click
    ack()
    
    # Extract poll and option IDs from the button value
    poll_id, option_id = body["actions"][0]["value"].split("|")
    user_id = body["user"]["id"]
    user_name = body["user"]["username"]
    
    # Get the poll
    poll = get_poll_by_id(poll_id)
    if not poll:
        logger.error(f"Poll not found: {poll_id}")
        return
    
    # Check if poll is already closed
    if poll.closed:
        try:
            client.chat_ephemeral(
                channel=body["channel"]["id"],
                user=user_id,
                text="This poll is already closed."
            )
        except SlackApiError as e:
            logger.error(f"Error sending ephemeral message: {e}")
        return
    
    # Check if user already voted
    user_votes = [vote for vote in poll.votes if vote.user_id == user_id]
    
    # If multiple votes aren't allowed and user already voted, remove the old vote
    if not poll.allow_multiple_votes and user_votes:
        # If trying to vote for the same option, remove the vote (toggle)
        if any(vote.option_id == option_id for vote in user_votes):
            for vote in user_votes[:]:
                if vote.option_id == option_id:
                    delete_vote(poll, vote)
        else:
            # If voting for a different option, remove old votes and add new one
            for vote in user_votes[:]:
                delete_vote(poll, vote)
            
            # Add new vote
            vote = Vote(user_id=user_id, user_name=user_name, option_id=option_id)
            save_vote(poll, vote)
    else:
        # Check if user already voted for this specific option
        existing_vote = next((vote for vote in user_votes if vote.option_id == option_id), None)
        
        if existing_vote:
            # Remove the vote (toggle behavior)
            delete_vote(poll, existing_vote)
        else:
            # Add new vote
            vote = Vote(user_id=user_id, user_name=user_name, option_id=option_id)
            save_vote(poll, vote)
    
    # Update the message with current vote counts
    try:
        client.chat_update(
            channel=body["container"]["channel_id"],
            ts=body["container"]["message_ts"],
            blocks=generate_poll_blocks(poll)
        )
    except SlackApiError as e:
        logger.error(f"Error updating poll message: {e}")

# Close poll button handler
@slack_app.action("close_poll")
def handle_close_poll(ack, body, client):
    # Acknowledge the button click
    ack()
    
    # Extract poll ID
    poll_id = body["actions"][0]["value"]
    user_id = body["user"]["id"]
    
    # Get the poll
    poll = get_poll_by_id(poll_id)
    if not poll:
        logger.error(f"Poll not found: {poll_id}")
        return
    
    # Check if user is the creator
    if poll.creator_id != user_id:
        try:
            client.chat_ephemeral(
                channel=body["channel"]["id"],
                user=user_id,
                text="Only the poll creator can close this poll."
            )
        except SlackApiError as e:
            logger.error(f"Error sending ephemeral message: {e}")
        return
    
    # Close the poll
    poll.closed = True
    save_poll(poll)
    
    # Update the message to show the poll is closed
    try:
        client.chat_update(
            channel=body["container"]["channel_id"],
            ts=body["container"]["message_ts"],
            blocks=generate_poll_blocks(poll)
        )
    except SlackApiError as e:
        logger.error(f"Error updating poll message: {e}")

# Show results button handler
@slack_app.action("show_results")
def handle_show_results(ack, body, client):
    # Acknowledge the button click
    ack()
    
    # Extract poll ID
    poll_id = body["actions"][0]["value"]
    user_id = body["user"]["id"]
    
    # Get the poll
    poll = get_poll_by_id(poll_id)
    if not poll:
        logger.error(f"Poll not found: {poll_id}")
        return
    
    # Check if user is the creator
    if poll.creator_id != user_id:
        try:
            client.chat_ephemeral(
                channel=body["channel"]["id"],
                user=user_id,
                text="Only the poll creator can show the results."
            )
        except SlackApiError as e:
            logger.error(f"Error sending ephemeral message: {e}")
        return
    
    # Post results as a new message
    try:
        client.chat_postMessage(
            channel=body["channel"]["id"],
            blocks=generate_results_blocks(poll),
            text=f"Poll Results: {poll.question}"  # Fallback text for notifications
        )
    except SlackApiError as e:
        logger.error(f"Error posting results: {e}")

# Flask routes
@app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)

@app.route("/", methods=["GET"])
def home():
    return render_template("index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
