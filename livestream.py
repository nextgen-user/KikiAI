import cv2
import numpy as np
from mss import mss
from flask import Flask, Response

app = Flask(__name__)

def generate_frames():
    # Initialize webcam capture
    cap = cv2.VideoCapture(0)
    
    # Initialize MSS for screen capture
    with mss() as sct:
        # Select the monitor to capture (1 is usually the primary)
        monitor = sct.monitors[1]

        try:
            while True:
                # 1. Capture the screen
                # sct_img is a raw pixels object
                img = sct.grab(monitor)
                
                # 2. Convert raw pixels to a numpy array for OpenCV
                frame = np.array(img)

                # 3. MSS captures in BGRA; OpenCV works better with BGR for encoding
                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

                # 4. Capture from webcam
                ret, cam_frame = cap.read()
                if ret:
                    # Dynamically resize the webcam frame to be an inset (e.g., 30% of screen height)
                    h_cam, w_cam, _ = cam_frame.shape
                    screen_h, screen_w, _ = frame.shape
                    
                    scale = (screen_h * 0.3) / h_cam
                    if scale > 0:
                        new_w, new_h = int(w_cam * scale), int(h_cam * scale)
                        cam_resized = cv2.resize(cam_frame, (new_w, new_h))
                        
                        # Optional: Draw a subtle border around the webcam inset
                        cv2.rectangle(cam_resized, (0, 0), (new_w-1, new_h-1), (200, 200, 200), 2)
                        
                        # Position it at the bottom right corner with a margin
                        margin = 20
                        y_offset = screen_h - new_h - margin
                        x_offset = screen_w - new_w - margin
                        
                        # Apply to frame (ensure it fits inside the screen dimensions)
                        if y_offset >= 0 and x_offset >= 0:
                            frame[y_offset:y_offset+new_h, x_offset:x_offset+new_w] = cam_resized

                # 5. Resize final stream to lower resolution/size before encoding for better performance
                frame = cv2.resize(frame, (0, 0), fx=0.5, fy=0.5)

                # 6. Encode the frame as a JPEG
                # Quality 70 is a good balance between speed and clarity
                success, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                
                if not success:
                    continue

                # 6. Yield the frame in the MJPEG multipart format
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
        finally:
            cap.release()

@app.route('/mjpeg')
def video_feed():
    # Returns the streaming response using the generator function
    return Response(generate_frames(), 
                    mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == "__main__":
    # Run the server on localhost at port 5000
    # Threaded=True is vital so the stream doesn't block the server
    app.run(host='0.0.0.0', port=5000, threaded=True)