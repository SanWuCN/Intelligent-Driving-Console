"""Local JPEG -> person boxes bridge for the Jetson Python 3 OpenCV runtime."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

import cv2
import numpy as np


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prototxt", required=True)
    parser.add_argument("--weights", required=True)
    parser.add_argument("--port", type=int, default=8771)
    args = parser.parse_args()
    cv2.setNumThreads(2)
    net = cv2.dnn.readNetFromCaffe(args.prototxt, args.weights)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/detect":
                self.send_error(404)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 2000000:
                    raise ValueError("invalid image size")
                image = cv2.imdecode(np.frombuffer(self.rfile.read(size), np.uint8), cv2.IMREAD_COLOR)
                if image is None:
                    raise ValueError("invalid JPEG")
                net.setInput(cv2.dnn.blobFromImage(image, 0.007843, (300, 300), 127.5))
                detections = net.forward().reshape(-1, 7)
                boxes = []
                for item in detections:
                    # VOC class 15 is person. Coordinates remain normalized.
                    if int(item[1]) == 15 and item[2] >= 0.5:
                        boxes.append({"confidence": float(item[2]),
                                      "box": np.clip(item[3:7], 0, 1).tolist()})
                body = json.dumps({"persons": boxes}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                self.send_error(503, str(exc))

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", args.port), Handler)
    server.timeout = 1
    server.serve_forever()


if __name__ == "__main__":
    main()
