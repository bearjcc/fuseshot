"""group.analyse / track with MediaPipe replaced by fakes."""

import numpy as np

from fuseshot import group


class FakeRec:
    def alignCrop(self, image, face):
        return np.full((112, 112, 3), 128, np.uint8)

    def feature(self, aligned):
        vec = np.ones(128, dtype=np.float32)
        vec[0] = float(face_slot(aligned))
        return vec

    class detector:
        pass


def face_slot(aligned):
    return aligned[0, 0, 0]


class FakeModels:
    def __init__(self):
        self.detector = group.FakeDetector if hasattr(group, "FakeDetector") else None
        self.recogniser = FakeRec()
        self.eyes_and_pose = lambda image, face: (0.8, 0.2, 5.0)


class FakeDet:
    def setInputSize(self, size):
        pass

    def detect(self, image):
        h, w = image.shape[:2]
        f = np.array([w * 0.4, h * 0.35, 60, 70, 0.95] + [0] * 10, dtype=np.float32)
        return 1, f[None]


def test_analyse_and_track_without_mediapipe(monkeypatch):
    images = [np.full((400, 500, 3), n * 30, np.uint8) for n in range(4)]
    models = FakeModels()
    models.detector = FakeDet()
    monkeypatch.setattr(group.action, "register", lambda imgs, ref: [np.eye(3) for _ in imgs])
    analysis = group.analyse(images, models, log=lambda *a: None)
    assert analysis["faces"] and analysis["tracks"]
    group.face_scores(analysis)
    assert all("score" in f for f in analysis["faces"])
