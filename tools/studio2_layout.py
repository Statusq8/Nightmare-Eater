"""Project a shared room model to depth and object-ID maps for fixed camera poses."""
import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

FACES = ((0,1,2,3),(4,5,6,7),(0,4,5,1),(1,5,6,2),(2,6,7,3),(3,7,4,0))
DIMENSIONS = {"slab": (.38,.27,.18), "wheelchair": (.19,.27,.20),
              "ceiling-bulb": (.045,.055,.045), "ward-door": (.19,.64,.02)}
CAMERAS = {"entrance": ((.50,.40,-.42),(.54,.34,.82)),
           "left": ((.19,.31,-.22),(.57,.34,.72)),
           "right": ((.84,.36,-.23),(.48,.32,.76))}
PALETTE = [(0,0,0), (35,45,58), (102,146,189), (172,115,49), (246,226,133), (138,49,66)]


def box(center, dimensions, yaw=0):
    x,y,z = (np.array(dimensions)/2)
    points = np.array([[-x,-y,-z],[x,-y,-z],[x,y,-z],[-x,y,-z],
                       [-x,-y,z],[x,-y,z],[x,y,z],[-x,y,z]])
    angle = np.deg2rad(yaw)
    c,s = np.cos(angle),np.sin(angle)
    return points @ np.array([[c,0,s],[0,1,0],[-s,0,c]]) + center


def rasterize(tri, depth, labels, ident):
    width, height = depth.shape[1], depth.shape[0]
    x0,y0 = np.maximum(np.floor(tri[:,:2].min(axis=0)).astype(int),0)
    x1,y1 = np.minimum(np.ceil(tri[:,:2].max(axis=0)).astype(int)+1,(width,height))
    if x0>=x1 or y0>=y1:
        return
    x,y = np.meshgrid(np.arange(x0,x1)+.5,np.arange(y0,y1)+.5)
    (ax,ay,az),(bx,by,bz),(cx,cy,cz) = tri
    determinant = (by-cy)*(ax-cx)+(cx-bx)*(ay-cy)
    if abs(determinant)<1e-7:
        return
    u = ((by-cy)*(x-cx)+(cx-bx)*(y-cy))/determinant
    v = ((cy-ay)*(x-cx)+(ax-cx)*(y-cy))/determinant
    w = 1-u-v
    with np.errstate(divide="ignore", invalid="ignore"):
        values = 1/(u/az+v/bz+w/cz)
    local = depth[y0:y1,x0:x1]
    inside = (u>=0)&(v>=0)&(w>=0)&(values<local)
    local[inside] = values[inside]
    labels[y0:y1,x0:x1][inside] = ident


def project(layout, camera, width=960, height=540):
    eye,target = map(np.asarray,CAMERAS[camera])
    forward = target-eye
    forward /= np.linalg.norm(forward)
    right = np.cross([0,1,0],forward)
    right /= np.linalg.norm(right)
    up = np.cross(forward,right)
    basis = np.column_stack((right,up,forward))
    depth = np.full((height,width),np.inf)
    labels = np.zeros((height,width),dtype=np.uint8)
    floor_x0,floor_z0,floor_x1,floor_z1 = layout["bounds"]
    if (floor_x0,floor_z0,floor_x1,floor_z1)!=(0,0,1,1):
        raise ValueError("Expected normalized 0–1 room bounds.")
    objects = [
        (1,box((.5,-.016,.50),(1,.03,1))),
        (1,box((.5,.35,1.025),(1,.70,.05))),
        (1,box((-.025,.35,.50),(.05,.70,1))),
        (1,box((1.025,.35,.50),(.05,.70,1))),
        (1,box((.50,.725,.50),(1,.05,1))),
    ]
    ids = {name: i for i,name in enumerate(DIMENSIONS,2)}
    if {item["key"] for item in layout["objects"]} != set(ids) or len(layout["objects"]) != len(ids):
        raise ValueError("Room must have one placement for each registered landmark.")
    for item in layout["objects"]:
        key = item["key"]
        dim = DIMENSIONS[key]
        y = .63 if key=="ceiling-bulb" else dim[1]/2
        objects.append((ids[key],box((item["x"],y,item["z"]),dim,item.get("orientation_deg",0))))
    for ident, points in objects:
        pts = (points-eye) @ basis
        if np.any(pts[:,2]<=0):
            raise ValueError("An object crosses the camera plane.")
        pts[:,0] = width/2 + height*.78*pts[:,0]/pts[:,2]
        pts[:,1] = height/2 - height*.78*pts[:,1]/pts[:,2]
        for a,b,c,d in FACES:
            rasterize(pts[[a,b,c]],depth,labels,ident)
            rasterize(pts[[a,c,d]],depth,labels,ident)
    return depth,labels,ids


def export(layout, destination):
    destination.mkdir(parents=True,exist_ok=True)
    report = {}
    for name in CAMERAS:
        depth,labels,ids = project(layout,name)
        grayscale = np.where(np.isfinite(depth),np.clip(1-(depth-.15)/1.5,0,1),0)
        Image.fromarray((grayscale*255).astype(np.uint8)).save(destination/f"{name}-depth.png")
        Image.fromarray(np.asarray(PALETTE,dtype=np.uint8)[labels]).save(destination/f"{name}-objects.png")
        scene = {}
        for key,ident in ids.items():
            y,x = np.where(labels==ident)
            scene[key] = {"pixels": len(x), "bounds": [int(x.min()),int(y.min()),int(x.max()),int(y.max())] if len(x) else None}
        report[name] = scene
    (destination/"geometry.json").write_text(json.dumps(report,indent=2),encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("layout",type=Path)
    parser.add_argument("destination",type=Path)
    args = parser.parse_args()
    export(json.loads(args.layout.read_text(encoding="utf-8")),args.destination)

