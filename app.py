import json
import logging
import os
import threading
import time
from datetime import datetime

import posthog
from flask import Flask, Response, jsonify, render_template, request
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from slack_sdk.errors import SlackApiError

from models import (
    Poll,
    Session,
    Vote,
    delete_vote,
    get_expired_polls,
    get_poll_by_id,
    save_poll,
    save_vote,
)

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET")
posthog.api_key = os.environ.get("POSTHOG_API_KEY")

# Initialize Slack app with bot token
# For development purposes, we can make the request verification more flexible
slack_app = App(
    token=os.environ.get("SLACK_BOT_TOKEN"),
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET"),
    process_before_response=True,
)


# Create a handler that will be used to process Slack events
class CustomSlackRequestHandler(SlackRequestHandler):
    def handle(self, req):
        # Override to handle verification errors more gracefully
        if "ssl_check" in req.form:
            # Handle SSL check from Slack
            return Response("OK", status=200, content_type="text/plain")

        return super().handle(req)


handler = CustomSlackRequestHandler(slack_app)


# Background thread to check for expired polls
def check_expired_polls():
    while True:
        try:
            # Use the get_expired_polls function instead of accessing Poll.polls
            expired_polls = get_expired_polls()

            for poll in expired_polls:
                logger.info(f"Automatically closing poll {poll.id} due to deadline")
                poll.closed = True
                save_poll(poll)

                # Update the message to reflect that the poll is closed
                try:
                    if poll.channel_id and poll.message_ts:
                        slack_app.client.chat_update(
                            channel=poll.channel_id,
                            ts=poll.message_ts,
                            blocks=generate_poll_blocks(poll),
                        )
                    else:
                        logger.error(
                            f"Missing channel_id or message_ts for poll {poll.id}"
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
            "text": {"type": "plain_text", "text": f"📊 {poll.question}"},
        },
        {"type": "divider"},
    ]

    # If poll is closed, show a notice
    if poll.closed:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "*This poll is closed*"}],
            }
        )

    # Add deadline if it exists
    if poll.deadline:
        deadline_str = poll.deadline.strftime("%Y-%m-%d %H:%M:%S")
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"*Deadline:* {deadline_str}"}],
            }
        )

    # Poll options section
    for option in poll.options:
        # Count votes for this option
        vote_count = sum(1 for vote in poll.votes if vote.option_id == option.id)

        # Show voters if not hidden
        voters_text = ""
        vote_count_text = f" - {vote_count} vote(s)"

        if not poll.hide_votes and vote_count > 0:
            voters = [
                vote.user_name for vote in poll.votes if vote.option_id == option.id
            ]
            voters_text = f" - Votes: {', '.join(voters)}"

        # Hide vote count if that option is enabled
        if poll.hide_vote_count:
            vote_count_text = ""

        # Create section block for option
        option_block = {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{option.text}*{vote_count_text}{voters_text}",
            },
        }

        # Only add vote button if poll is not closed
        if not poll.closed:
            option_block["accessory"] = {
                "type": "button",
                "text": {"type": "plain_text", "text": "Vote"},
                "value": f"{poll.id}|{option.id}",
                "action_id": "vote_button",
                "style": "primary",
            }

        blocks.append(option_block)

    # Add controls for poll creator
    if not poll.closed:
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Close Poll"},
                        "value": poll.id,
                        "action_id": "close_poll",
                        "style": "danger",
                    }
                ],
            }
        )
    else:
        # If poll is closed, add show results button
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Show Results"},
                        "value": poll.id,
                        "action_id": "show_results",
                    }
                ],
            }
        )

    return blocks


# Function to generate results blocks
def generate_results_blocks(poll):
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📊 Results: {poll.question}"},
        },
        {"type": "divider"},
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
        reverse=True,
    )

    # Add each option with vote count
    for option, count in sorted_options:
        voters_text = ""
        vote_count_text = f": {count} vote(s)"

        if not poll.hide_votes and count > 0:
            voters = [
                vote.user_name for vote in poll.votes if vote.option_id == option.id
            ]
            voters_text = f"\nVoters: {', '.join(voters)}"

        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{option.text}*{vote_count_text}{voters_text}",
                },
            }
        )

    return blocks


# Slash command handler for creating a poll
@slack_app.command("/poll")
def create_poll(ack, body, client):
    # Acknowledge the command request
    ack()

    # Get channel_id
    channel_id = body["channel_id"]

    # Open a modal for poll creation
    try:
        client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "poll_creation_modal",
                "title": {"type": "plain_text", "text": "Create a Poll"},
                "submit": {"type": "plain_text", "text": "Create"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "question_block",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "question",
                            "placeholder": {
                                "type": "plain_text",
                                "text": "What would you like to know?",
                            },
                        },
                        "label": {"type": "plain_text", "text": "Poll Question"},
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
                                "text": "Enter one option per line",
                            },
                        },
                        "label": {"type": "plain_text", "text": "Poll Options"},
                    },
                    {
                        "type": "input",
                        "block_id": "deadline_block",
                        "optional": True,
                        "element": {"type": "datepicker", "action_id": "deadline_date"},
                        "label": {
                            "type": "plain_text",
                            "text": "Deadline Date (Optional)",
                        },
                    },
                    {
                        "type": "input",
                        "block_id": "deadline_time_block",
                        "optional": True,
                        "element": {"type": "timepicker", "action_id": "deadline_time"},
                        "label": {
                            "type": "plain_text",
                            "text": "Deadline Time (Optional)",
                        },
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
                                        "text": "Allow multiple votes per user",
                                    },
                                    "value": "multiple_votes",
                                },
                                {
                                    "text": {
                                        "type": "plain_text",
                                        "text": "Hide individual votes until poll is closed",
                                    },
                                    "value": "hide_votes",
                                },
                                {
                                    "text": {
                                        "type": "plain_text",
                                        "text": "Hide vote count until poll is closed",
                                    },
                                    "value": "hide_vote_count",
                                },
                            ],
                        },
                        "label": {"type": "plain_text", "text": "Poll Settings"},
                    },
                ],
                "private_metadata": channel_id,
            },
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
        if (
            view["state"]["values"]["deadline_block"]["deadline_date"]["selected_date"]
            and view["state"]["values"]["deadline_time_block"]["deadline_time"][
                "selected_time"
            ]
        ):
            deadline_date = view["state"]["values"]["deadline_block"]["deadline_date"][
                "selected_date"
            ]
            deadline_time = view["state"]["values"]["deadline_time_block"][
                "deadline_time"
            ]["selected_time"]

            # Parse date and time and create datetime object
            deadline_str = f"{deadline_date} {deadline_time}"
            deadline = datetime.strptime(deadline_str, "%Y-%m-%d %H:%M")
    except Exception as e:
        logger.error(f"Error parsing deadline: {e}")

    # Parse settings
    settings = view["state"]["values"]["settings_block"]["settings"]["selected_options"]
    allow_multiple_votes = (
        any(item["value"] == "multiple_votes" for item in settings)
        if settings
        else False
    )
    hide_votes = (
        any(item["value"] == "hide_votes" for item in settings) if settings else False
    )
    hide_vote_count = (
        any(item["value"] == "hide_vote_count" for item in settings)
        if settings
        else False
    )

    # Make sure vote counts can't be hidden if individual votes are visible
    if hide_votes is False and hide_vote_count is True:
        hide_vote_count = False

    # Split options by newline
    option_texts = [opt.strip() for opt in options_text.split("\n") if opt.strip()]

    # Create poll object
    poll = Poll(
        question=question,
        creator_id=body["user"]["id"],
        allow_multiple_votes=allow_multiple_votes,
        hide_votes=hide_votes,
        hide_vote_count=hide_vote_count,
        deadline=deadline,
        channel_id=view["private_metadata"],
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
            text=f"Poll: {question}",  # Fallback text for notifications
        )
        posthog.capture(
            "poll_created", properties={"poll_id": poll.id, "user_id": poll.creator_id}
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
                text="This poll is already closed.",
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
        existing_vote = next(
            (vote for vote in user_votes if vote.option_id == option_id), None
        )

        if existing_vote:
            # Remove the vote (toggle behavior)
            delete_vote(poll, existing_vote)
        else:
            # Add new vote
            vote = Vote(user_id=user_id, user_name=user_name, option_id=option_id)
            save_vote(poll, vote)
            posthog.capture(
                "poll_vote_submitted",
                properties={"poll_id": poll.id, "user_id": user_id},
            )

    # Update the message with current vote counts
    try:
        client.chat_update(
            channel=body["container"]["channel_id"],
            ts=body["container"]["message_ts"],
            blocks=generate_poll_blocks(poll),
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
                text="Only the poll creator can close this poll.",
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
            blocks=generate_poll_blocks(poll),
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
                text="Only the poll creator can show the results.",
            )
        except SlackApiError as e:
            logger.error(f"Error sending ephemeral message: {e}")
        return

    # Post results as a new message
    try:
        client.chat_postMessage(
            channel=body["channel"]["id"],
            blocks=generate_results_blocks(poll),
            text=f"Poll Results: {poll.question}",  # Fallback text for notifications
        )
    except SlackApiError as e:
        logger.error(f"Error posting results: {e}")


# Flask routes
@app.route("/slack/events", methods=["POST"])
def slack_events():
    # Print request details for debugging
    logger.info(f"Received request headers: {request.headers}")

    # Check for interactivity payload
    if request.form and "payload" in request.form:
        logger.info("Received interactive payload")
        try:
            payload = json.loads(request.form["payload"])
            logger.info(f"Payload type: {payload.get('type')}")

            # Handle different types of interactions
            if (
                payload.get("type") == "view_submission"
                and payload.get("view", {}).get("callback_id") == "poll_creation_modal"
            ):
                logger.info("Handling poll creation modal submission")
                # Manually handle poll submission
                try:
                    # Extract values from the modal
                    view = payload.get("view", {})
                    state = view.get("state", {}).get("values", {})

                    question = (
                        state.get("question_block", {}).get("question", {}).get("value")
                    )
                    options_text = (
                        state.get("options_block", {}).get("options", {}).get("value")
                    )

                    # Parse deadline if provided
                    deadline = None
                    try:
                        deadline_date = (
                            state.get("deadline_block", {})
                            .get("deadline_date", {})
                            .get("selected_date")
                        )
                        deadline_time = (
                            state.get("deadline_time_block", {})
                            .get("deadline_time", {})
                            .get("selected_time")
                        )

                        if deadline_date and deadline_time:
                            # Parse date and time and create datetime object
                            deadline_str = f"{deadline_date} {deadline_time}"
                            deadline = datetime.strptime(deadline_str, "%Y-%m-%d %H:%M")
                    except Exception as e:
                        logger.error(f"Error parsing deadline: {e}")

                    # Parse settings
                    settings = (
                        state.get("settings_block", {})
                        .get("settings", {})
                        .get("selected_options", [])
                    )
                    allow_multiple_votes = any(
                        item.get("value") == "multiple_votes" for item in settings
                    )
                    hide_votes = any(
                        item.get("value") == "hide_votes" for item in settings
                    )
                    hide_vote_count = any(
                        item.get("value") == "hide_vote_count" for item in settings
                    )

                    # Make sure vote counts can't be hidden if individual votes are visible
                    if not hide_votes and hide_vote_count:
                        hide_vote_count = False

                    # Split options by newline
                    option_texts = [
                        opt.strip() for opt in options_text.split("\n") if opt.strip()
                    ]

                    # Create poll object
                    poll = Poll(
                        question=question,
                        creator_id=payload.get("user", {}).get("id"),
                        allow_multiple_votes=allow_multiple_votes,
                        hide_votes=hide_votes,
                        hide_vote_count=hide_vote_count,
                        deadline=deadline,
                        channel_id=view.get("private_metadata"),
                    )

                    # Add options
                    for text in option_texts:
                        poll.add_option(text)

                    # Save poll
                    save_poll(poll)

                    # Post the poll to the channel
                    try:
                        if poll.channel_id:
                            result = slack_app.client.chat_postMessage(
                                channel=poll.channel_id,
                                blocks=generate_poll_blocks(poll),
                                text=f"Poll: {question}",  # Fallback text for notifications
                            )

                            # Store the message timestamp for later updates
                            poll.message_ts = result["ts"]
                            save_poll(poll)
                        else:
                            logger.error("Cannot post poll: channel ID is missing")
                    except Exception as e:
                        logger.error(f"Error posting poll: {e}")
                except Exception as e:
                    logger.error(f"Error processing poll submission: {e}")
                return ""

            elif payload.get("type") == "block_actions":
                # Handle button clicks and other block actions
                action_id = (
                    payload.get("actions", [{}])[0].get("action_id")
                    if payload.get("actions")
                    else None
                )
                logger.info(f"Handling block action: {action_id}")

                # Handle the different action types directly
                if action_id == "vote_button":
                    logger.info("Processing vote")
                    try:
                        # Extract poll and option IDs from the button value
                        poll_id, option_id = (
                            payload.get("actions", [{}])[0].get("value", "").split("|")
                        )
                        user_id = payload.get("user", {}).get("id")
                        user_name = payload.get("user", {}).get("username", "unknown")

                        # Get the poll
                        poll = get_poll_by_id(poll_id)
                        if not poll:
                            logger.error(f"Poll not found: {poll_id}")
                            return ""

                        # Check if poll is already closed
                        if poll.closed:
                            try:
                                channel_id = payload.get("channel", {}).get("id")
                                if channel_id and user_id:
                                    slack_app.client.chat_postEphemeral(
                                        channel=channel_id,
                                        user=user_id,
                                        text="This poll is already closed.",
                                    )
                            except Exception as e:
                                logger.error(f"Error sending ephemeral message: {e}")
                            return ""

                        # Check if user already voted
                        user_votes = [
                            vote for vote in poll.votes if vote.user_id == user_id
                        ]

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
                                vote = Vote(
                                    user_id=user_id,
                                    user_name=user_name,
                                    option_id=option_id,
                                )
                                save_vote(poll, vote)
                        else:
                            # Check if user already voted for this specific option
                            existing_vote = next(
                                (
                                    vote
                                    for vote in user_votes
                                    if vote.option_id == option_id
                                ),
                                None,
                            )

                            if existing_vote:
                                # Remove the vote (toggle behavior)
                                delete_vote(poll, existing_vote)
                            else:
                                # Add new vote
                                vote = Vote(
                                    user_id=user_id,
                                    user_name=user_name,
                                    option_id=option_id,
                                )
                                save_vote(poll, vote)

                        # Update the message with current vote counts
                        try:
                            slack_app.client.chat_update(
                                channel=payload.get("container", {}).get("channel_id"),
                                ts=payload.get("container", {}).get("message_ts"),
                                blocks=generate_poll_blocks(poll),
                            )
                        except Exception as e:
                            logger.error(f"Error updating poll message: {e}")
                    except Exception as e:
                        logger.error(f"Error processing vote: {e}")

                elif action_id == "close_poll":
                    logger.info("Processing close poll request")
                    try:
                        # Extract poll ID
                        poll_id = payload.get("actions", [{}])[0].get("value")
                        user_id = payload.get("user", {}).get("id")

                        # Get the poll
                        poll = get_poll_by_id(poll_id)
                        if not poll:
                            logger.error(f"Poll not found: {poll_id}")
                            return ""

                        # Check if user is the creator
                        if poll.creator_id != user_id:
                            try:
                                channel_id = payload.get("channel", {}).get("id")
                                if channel_id and user_id:
                                    slack_app.client.chat_postEphemeral(
                                        channel=channel_id,
                                        user=user_id,
                                        text="Only the poll creator can close this poll.",
                                    )
                            except Exception as e:
                                logger.error(f"Error sending ephemeral message: {e}")
                            return ""

                        # Close the poll
                        poll.closed = True
                        save_poll(poll)

                        # Update the message to show the poll is closed
                        try:
                            slack_app.client.chat_update(
                                channel=payload.get("container", {}).get("channel_id"),
                                ts=payload.get("container", {}).get("message_ts"),
                                blocks=generate_poll_blocks(poll),
                            )
                        except Exception as e:
                            logger.error(f"Error updating poll message: {e}")
                    except Exception as e:
                        logger.error(f"Error closing poll: {e}")

                elif action_id == "show_results":
                    logger.info("Processing show results request")
                    try:
                        # Extract poll ID
                        poll_id = payload.get("actions", [{}])[0].get("value")

                        # Get the poll
                        poll = get_poll_by_id(poll_id)
                        if not poll:
                            logger.error(f"Poll not found: {poll_id}")
                            return ""

                        # Post results message
                        try:
                            channel_id = payload.get("channel", {}).get("id")
                            if channel_id:
                                slack_app.client.chat_postMessage(
                                    channel=channel_id,
                                    blocks=generate_results_blocks(poll),
                                    text=f"Poll Results: {poll.question}",  # Fallback text for notifications
                                )
                            else:
                                logger.error(
                                    "Cannot post results: channel ID is missing"
                                )
                        except Exception as e:
                            logger.error(f"Error posting results: {e}")
                    except Exception as e:
                        logger.error(f"Error showing results: {e}")

                return ""
        except Exception as e:
            logger.error(f"Error handling interactive payload: {e}")
            return ""

    # Log form data
    form_data = request.form.to_dict() if request.form else {}
    # For security, don't log the entire token
    if "token" in form_data:
        form_data["token"] = (
            form_data["token"][:5] + "..." if form_data["token"] else "None"
        )
    logger.info(f"Request form data: {form_data}")

    # Directly handle SSL check from Slack
    if request.form and "ssl_check" in request.form:
        logger.info("Handling SSL check request")
        return "OK"

    # Handle slash command directly for testing
    if (
        request.form
        and "command" in request.form
        and request.form["command"] == "/poll"
    ):
        logger.info("Received /poll command")
        user_id = request.form["user_id"]
        channel_id = request.form["channel_id"]
        trigger_id = request.form["trigger_id"]

        try:
            # Open poll creation modal
            slack_app.client.views_open(
                trigger_id=trigger_id,
                view={
                    "type": "modal",
                    "callback_id": "poll_creation_modal",
                    "title": {"type": "plain_text", "text": "Create a Poll"},
                    "submit": {"type": "plain_text", "text": "Create"},
                    "close": {"type": "plain_text", "text": "Cancel"},
                    "blocks": [
                        {
                            "type": "input",
                            "block_id": "question_block",
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "question",
                                "placeholder": {
                                    "type": "plain_text",
                                    "text": "What would you like to know?",
                                },
                            },
                            "label": {"type": "plain_text", "text": "Poll Question"},
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
                                    "text": "Enter one option per line",
                                },
                            },
                            "label": {"type": "plain_text", "text": "Poll Options"},
                        },
                        {
                            "type": "input",
                            "block_id": "deadline_block",
                            "optional": True,
                            "element": {
                                "type": "datepicker",
                                "action_id": "deadline_date",
                            },
                            "label": {
                                "type": "plain_text",
                                "text": "Deadline Date (Optional)",
                            },
                        },
                        {
                            "type": "input",
                            "block_id": "deadline_time_block",
                            "optional": True,
                            "element": {
                                "type": "timepicker",
                                "action_id": "deadline_time",
                            },
                            "label": {
                                "type": "plain_text",
                                "text": "Deadline Time (Optional)",
                            },
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
                                            "text": "Allow multiple votes per user",
                                        },
                                        "value": "multiple_votes",
                                    },
                                    {
                                        "text": {
                                            "type": "plain_text",
                                            "text": "Hide individual votes until poll is closed",
                                        },
                                        "value": "hide_votes",
                                    },
                                    {
                                        "text": {
                                            "type": "plain_text",
                                            "text": "Hide vote count until poll is closed",
                                        },
                                        "value": "hide_vote_count",
                                    },
                                ],
                            },
                            "label": {"type": "plain_text", "text": "Poll Settings"},
                        },
                    ],
                    "private_metadata": channel_id,
                },
            )
            return ""  # Empty 200 response to acknowledge
        except Exception as e:
            logger.error(f"Error opening modal: {e}")
            return f"Error: {str(e)}", 200

    # Fall back to the handler for other events
    try:
        return handler.handle(request)
    except Exception as e:
        logger.error(f"Error handling request: {e}")
        return (
            f"Error: {str(e)}",
            200,
        )  # Return 200 even on error to prevent Slack retries


@app.route("/", methods=["GET"])
def home():
    return render_template("index.html")


@app.route("/health", methods=["GET"])
def health_check():
    """
    Simple health check endpoint that also displays the current Slack app configuration
    """
    # Count polls in the database
    session = Session()
    polls_count = session.query(Poll).count()

    return jsonify(
        {
            "status": "ok",
            "slack_app": {
                "token_set": bool(os.environ.get("SLACK_BOT_TOKEN")),
                "signing_secret_set": bool(os.environ.get("SLACK_SIGNING_SECRET")),
                "channel_id_set": bool(os.environ.get("SLACK_CHANNEL_ID")),
                "webhook_url": request.host_url + "slack/events",
            },
            "polls_count": polls_count,
        }
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
