import os
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import pusher
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
import requests
from supabase import create_client
import time  # Import time to generate unique timestamps

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
CORS(app)  # Enable CORS

# Initialize Pusher client with credentials from the environment
pusher_client = pusher.Pusher(
  app_id=os.getenv('PUSHER_APP_ID'),
  key=os.getenv('PUSHER_KEY'),
  secret=os.getenv('PUSHER_SECRET'),
  cluster=os.getenv('PUSHER_CLUSTER', 'ap2'),
  ssl=True
)

# Initialize Supabase client
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json"
}

# Directory to store uploaded images
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

@app.route("/upload", methods=["POST"])
def upload_image():
    try:
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"error": "No selected file"}), 400

        allowed_types = {'image/jpeg', 'image/png', 'image/gif', 'video/mp4', 'video/webm', 'video/ogg'}
        if file.content_type not in allowed_types:
            return jsonify({"error": "File type not allowed"}), 400

        # Generate unique filename
        original_filename = secure_filename(file.filename)
        unique_filename = f"{int(time.time())}_{original_filename}"
        
        # Choose the appropriate folder based on file type
        if file.content_type.startswith('image/'):
            file_path = f"images/{unique_filename}"
        else:
            file_path = f"videos/{unique_filename}"

        # Read and upload file to Supabase
        file_content = file.read()
        try:
            supabase.storage.from_("chat-images").upload(file_path, file_content)
        except Exception as upload_error:
            print(f"Upload error: {upload_error}")
            return jsonify({"error": "Failed to upload file"}), 500

        # Get public URL
        try:
            public_url = supabase.storage.from_("chat-images").get_public_url(file_path)
            # Add file type information to the response
            return jsonify({
                "url": public_url,
                "file_type": "video" if file.content_type.startswith('video/') else "image"
            }), 200
        except Exception as url_error:
            print(f"URL generation error: {url_error}")
            return jsonify({"error": "Failed to generate file URL"}), 500

    except Exception as e:
        print(f"General error: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/uploads/<filename>", methods=["GET"])
def get_image(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

@app.route("/messages", methods=["POST"])
def send_message():
    try:
        data = request.get_json()
        if not data or "username" not in data or "group" not in data:
            return jsonify({"error": "Missing username or group"}), 400

        file_url = data.get("file_url")
        file_type = data.get("file_type")

        # Create payload with the required fields
        payload = {
            "username": data["username"],
            "message": data.get("message", ""),
            "group_name": data["group"],
            "created_at": data.get("created_at", time.strftime("%Y-%m-%dT%H:%M:%SZ"))
        }
        
        # Handle media URLs based on file_type
        if file_url:
            if file_type == "video":
                try:
                    # Try to use the new column schema
                    test_payload = payload.copy()
                    test_payload["video_url"] = file_url
                    
                    # Test if video_url column exists by sending a small request
                    test_response = requests.post(
                        f"{SUPABASE_URL}/rest/v1/messages",
                        json=test_payload,
                        headers={
                            "apikey": SUPABASE_KEY,
                            "Authorization": f"Bearer {SUPABASE_KEY}",
                            "Content-Type": "application/json",
                            "Prefer": "return=representation"
                        }
                    )
                    
                    if test_response.status_code == 201:
                        # Success! The column exists and the message is saved
                        saved_message = test_response.json()[0]
                        pusher_client.trigger(f"chat-channel-{data['group']}", "new-message", saved_message)
                        return jsonify({"success": True, "message": saved_message}), 200
                    else:
                        # Column doesn't exist, store video URL in image_url as fallback
                        payload["image_url"] = file_url
                        # Add a marker to identify it as a video
                        payload["message"] += "\n[VIDEO]"
                        
                except Exception as e:
                    print(f"Error testing video_url column: {str(e)}")
                    # Fallback to using image_url for videos
                    payload["image_url"] = file_url
                    payload["message"] += "\n[VIDEO]"
            else:
                # For images, use the image_url field
                payload["image_url"] = file_url

        # Debug print
        print("Sending payload to Supabase:", payload)
        
        response = requests.post(
            f"{SUPABASE_URL}/rest/v1/messages",
            json=payload,
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=representation"
            }
        )
        
        if response.status_code != 201:
            print(f"Supabase error: Status {response.status_code}, Response: {response.text}")
            return jsonify({"error": "Failed to save message", "details": response.text}), 500

        # Trigger Pusher event with the saved message
        saved_message = response.json()[0]
        
        # Add file_type to the saved message for frontend to identify it correctly
        if file_url:
            saved_message["file_type"] = file_type
        
        pusher_client.trigger(f"chat-channel-{data['group']}", "new-message", saved_message)
        return jsonify({"success": True, "message": saved_message}), 200

    except Exception as e:
        print(f"Error in send_message: {str(e)}")
        return jsonify({"error": "Failed to process message", "details": str(e)}), 500

# Make sure your messages table has these columns in Supabase:
# - id (int8, primary key)
# - username (text)
# - message (text)
# - group_name (text)
# - image_url (text)
# - video_url (text)
# - created_at (timestamptz, default: now())

@app.route("/messages/<group>", methods=["GET"])
def get_messages(group):
    try:
        # Retrieve past messages for the specified group from Supabase
        response = requests.get(
            f"{SUPABASE_URL}/rest/v1/messages?group_name=eq.{group}&order=created_at.asc",
            headers=SUPABASE_HEADERS
        )
        if response.status_code != 200:
            return jsonify({"error": "Failed to fetch messages"}), 500

        messages = response.json()
        for message in messages:
            if "id" not in message:
                print(f"Warning: Message missing `id` field: {message}")
                message["id"] = None
            
            # Check for video marker in the message text
            is_video = False
            if message.get("message") and "[VIDEO]" in message["message"]:
                is_video = True
                # Clean up the message text
                message["message"] = message["message"].replace("\n[VIDEO]", "")
            
            # Set file_type based on available URLs and markers
            if "video_url" in message and message["video_url"]:
                message["file_type"] = "video"
            elif "image_url" in message and message["image_url"]:
                if is_video:
                    # This is actually a video stored in image_url
                    message["file_type"] = "video"
                    # For frontend consistency, set video_url
                    message["video_url"] = message["image_url"]
                else:
                    message["file_type"] = "image"

        return jsonify(messages), 200
    except Exception as e:
        print(f"Error in get_messages: {str(e)}")
        return jsonify({"error": "Internal server error", "details": str(e)}), 500

@app.route("/messages/<int:message_id>", methods=["DELETE"])
def delete_message(message_id):
    try:
        username = request.args.get("username")
        if not username:
            return jsonify({"error": "Username is required"}), 400

        # Check if the message exists and belongs to the user
        response = requests.get(
            f"{SUPABASE_URL}/rest/v1/messages?id=eq.{message_id}",
            headers=SUPABASE_HEADERS
        )
        if response.status_code != 200 or not response.json():
            return jsonify({"error": "Message not found"}), 404

        message = response.json()[0]
        if message["username"] != username:
            return jsonify({"error": "You can only delete your own messages"}), 403

        # Delete the message
        delete_response = requests.delete(
            f"{SUPABASE_URL}/rest/v1/messages?id=eq.{message_id}",
            headers=SUPABASE_HEADERS
        )
        if delete_response.status_code != 204:
            return jsonify({"error": "Failed to delete message"}), 500

        # Trigger Pusher event to notify clients about the deletion
        pusher_client.trigger(f"chat-channel-{message['group_name']}", "delete-message", {"id": message_id})

        return jsonify({"success": True}), 200

    except Exception as e:
        print(f"Error in delete_message: {str(e)}")
        return jsonify({"error": "Internal server error", "details": str(e)}), 500

# Routes for managing groups
@app.route("/groups", methods=["GET"])
def get_groups():
    try:
        # Get the default groups
        default_groups = [
            "general","health", "finance", "education", "science", "business",
            "travel", "productivity", "ai-tech", "cybersecurity",
            "mental-health", "personal-development", "news-politics",
            "investing", "self-care", "career-growth"
        ]
        
        # Retrieve custom groups from Supabase
        response = requests.get(
            f"{SUPABASE_URL}/rest/v1/groups",
            headers=SUPABASE_HEADERS
        )
        
        if response.status_code == 200:
            custom_groups = [group["name"] for group in response.json()]
            all_groups = default_groups + custom_groups
            return jsonify({"groups": all_groups}), 200
        else:
            # If there's an error retrieving custom groups, just return the defaults
            return jsonify({"groups": default_groups}), 200
            
    except Exception as e:
        print(f"Error retrieving groups: {str(e)}")
        return jsonify({"error": "Failed to retrieve groups", "details": str(e)}), 500

@app.route("/groups", methods=["POST"])
def create_group():
    try:
        data = request.get_json()
        if not data or "name" not in data or "username" not in data:
            return jsonify({"error": "Group name and username are required"}), 400
            
        group_name = data["name"].strip()
        username = data["username"].strip()
        if not group_name:
            return jsonify({"error": "Group name cannot be empty"}), 400
        
        print(f"Creating group with name: {group_name} by user: {username}")
        
        # Check if the group already exists using Supabase client
        try:
            existing = supabase.table('groups').select('*').eq('name', group_name).execute()
            if existing.data:
                return jsonify({"error": "Group already exists"}), 409
        except Exception as e:
            print(f"Error checking if group exists: {str(e)}")
        
        # Create the group using Supabase client
        try:
            result = supabase.table('groups').insert({
                'name': group_name,
                'created_by': username,  # Store the username who created the group
                'created_at': time.strftime("%Y-%m-%dT%H:%M:%SZ")
            }).execute()
            
            if result.data:
                return jsonify({"success": True, "group": result.data[0]}), 201
            else:
                return jsonify({"error": "Failed to create group"}), 500
                
        except Exception as insert_error:
            print(f"Error inserting group: {str(insert_error)}")
            return jsonify({"error": "Exception during group insertion", "details": str(insert_error)}), 500
            
    except Exception as e:
        print(f"Error creating group: {str(e)}")
        return jsonify({"error": "Failed to create group", "details": str(e)}), 500

@app.route("/groups/<group_name>", methods=["DELETE"])
def delete_group(group_name):
    try:
        username = request.args.get("username")
        if not username:
            return jsonify({"error": "Username is required"}), 400

        # Check if the group exists
        response = supabase.table('groups').select('*').eq('name', group_name).execute()
        if not response.data:
            return jsonify({"error": "Group not found"}), 404
            
        # Check if user is authorized to delete this group
        group = response.data[0]
        if 'created_by' in group and group['created_by'] != username:
            return jsonify({"error": "You can only delete groups you created"}), 403

        # Delete the group
        delete_response = supabase.table('groups').delete().eq('name', group_name).execute()
        
        if delete_response and hasattr(delete_response, 'data') and delete_response.data:
            return jsonify({"success": True}), 200
        else:
            return jsonify({"error": "Failed to delete group"}), 500
            
    except Exception as e:
        print(f"Error deleting group: {str(e)}")
        return jsonify({"error": "Failed to delete group", "details": str(e)}), 500

# Add this setup endpoint for creating the groups table if needed
@app.route("/setup/groups-table", methods=["POST"])
def setup_groups_table():
    """
    Endpoint to create the groups table if it doesn't exist.
    This is a utility endpoint that should be called once to set up the database.
    """
    try:
        # SQL to create the groups table
        sql = """
        CREATE TABLE IF NOT EXISTS groups (
          id bigint primary key generated by default as identity,
          name text not null unique,
          created_by text not null, 
          created_at timestamptz default now()
        );
        """
        
        # Execute SQL via the REST API
        response = requests.post(
            f"{SUPABASE_URL}/rest/v1/rpc/execute_sql",
            json={"query": sql},
            headers=SUPABASE_HEADERS
        )
        
        if response.status_code == 200:
            return jsonify({"success": True, "message": "Groups table created or already exists"}), 200
        else:
            return jsonify({"error": "Failed to create table", "details": response.text}), 500
    
    except Exception as e:
        print(f"Error setting up groups table: {str(e)}")
        return jsonify({"error": "Failed to set up groups table", "details": str(e)}), 500

@app.route("/messages/<int:message_id>/react", methods=["POST"])
def react_to_message(message_id):
    try:
        data = request.get_json()
        if not data or "reaction" not in data or "username" not in data:
            return jsonify({"error": "Reaction and username are required"}), 400

        reaction = data["reaction"]
        username = data["username"]
        group = data.get("group", "general")  # Changed default from "general" to "health"

        # Create payload for database
        payload = {
            "message_id": message_id,
            "username": username,
            "reaction": reaction,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ")
        }
        
        # Save reaction to database
        try:
            result = supabase.table('reactions').insert(payload).execute()
            if not result.data:
                return jsonify({"error": "Failed to save reaction"}), 500
            
            # Get the saved reaction with ID
            saved_reaction = result.data[0]

            # Broadcast reaction via Pusher
            pusher_client.trigger(f"chat-channel-{group}", "reaction-update", {
                "message_id": message_id,
                "username": username,
                "reaction": reaction,
                "id": saved_reaction["id"]
            })
            
            return jsonify({"success": True, "reaction": saved_reaction}), 201
        except Exception as e:
            print(f"Error saving reaction: {str(e)}")
            return jsonify({"error": "Failed to process reaction", "details": str(e)}), 500

    except Exception as e:
        print(f"Error in react_to_message: {str(e)}")
        return jsonify({"error": "Internal server error", "details": str(e)}), 500

@app.route("/messages/<int:message_id>/reactions", methods=["GET"])
def get_message_reactions(message_id):
    try:
        # Retrieve all reactions for the specified message
        response = requests.get(
            f"{SUPABASE_URL}/rest/v1/reactions?message_id=eq.{message_id}&order=created_at.asc",
            headers=SUPABASE_HEADERS
        )
        
        if response.status_code != 200:
            return jsonify({"error": "Failed to fetch reactions"}), 500
            
        reactions = response.json()
        return jsonify(reactions), 200
    except Exception as e:
        print(f"Error retrieving reactions: {str(e)}")
        return jsonify({"error": "Failed to retrieve reactions", "details": str(e)}), 500

if __name__ == "__main__":
    # app.run(debug=True, port=5000)
    # # if __name__ == "__main__":
    from waitress import serve
    serve(app, host="0.0.0.0", port=5000)