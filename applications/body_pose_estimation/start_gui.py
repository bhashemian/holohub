import os
import sys

# Import BodyPoseEstimationApp
from body_pose_estimation import BodyPoseEstimationApp

# PySide6 for GUI
from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QLabel,
    QMainWindow,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtGui import QFont

# Global variable to hold the Holoscan application instance
gApp = None

# Worker class to run the Holoscan application in a separate thread
class HoloscanWorker(QObject):
    finished = Signal()  # Signal to indicate the worker has finished
    progress = Signal(int)  # Signal to indicate progress (if needed)
    def run(self):
        """Run the Holoscan application."""
        config_file = os.path.join(os.path.dirname(__file__), "body_pose_estimation.yaml")
        global gApp
        gApp = app = BodyPoseEstimationApp(source="v4l2")
        app.config(config_file)
        app.run()


# Main window class for the PySide2 UI
class Window(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUi()  # Setup the UI
        self.runHoloscanApp()  # Run the Holoscan application

    def setupUi(self):
        """Setup the UI components."""
        self.setWindowTitle("Body Pose Estimation")

        self.resize(400, 100)
        font = QFont("Arial", 15)

        self.centralWidget = QWidget()
        self.setCentralWidget(self.centralWidget)
        layout = QVBoxLayout()

        # Create the checkbox to label visibility
        self.boxes = QCheckBox("boxes")
        layout.addWidget(self.boxes)
        self.boxes.setChecked(True)
        self.boxes.setFont(font)
        self.boxes.stateChanged.connect(self.on_state_changed)

        # Create the checkbox to label visibility
        self.bodypose = QCheckBox("bodypose")
        layout.addWidget(self.bodypose)
        self.bodypose.setChecked(True)
        self.bodypose.setFont(font)
        self.bodypose.stateChanged.connect(self.on_state_changed)

        self.centralWidget.setLayout(layout)

    def on_state_changed(self):
        """Handle the submit button click event."""
        visible = set()
        if self.boxes.isChecked():
            visible.add("boxes")
        if self.bodypose.isChecked():
            visible.add("bodypose")

        # Set parameters in the Holoscan application
        global gApp
        if gApp:
            gApp.set_parameters(visible)

    def runHoloscanApp(self):
        """Run the Holoscan application in a separate thread."""
        self.thread = QThread()
        self.worker = HoloscanWorker()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def closeEvent(self, event):
        self.thread.quit()
        print("Note: To close this application, please close the Holoviz window first.")
        self.thread.wait()
        self.close()

    def keyPressEvent(self, event):
        """Handle key press events."""
        if event.key() == Qt.Key_Escape:
            self.thread.quit()
            self.thread.wait()
            self.close()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = Window()
    win.show()
    sys.exit(app.exec_())
