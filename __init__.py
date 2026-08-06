# -*- coding: utf-8 -*-
def classFactory(iface):
    from .three_d_track_viewer import ThreeDTrackViewer
    return ThreeDTrackViewer(iface)
