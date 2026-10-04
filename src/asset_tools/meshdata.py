"""
meshdata.py -- the skinned model as plain numpy arrays, for the 3D preview.

The editor's preview needs the same geometry the glTF exporter already proved
it can read: real vertex streams off the uncompressed Mesh, JOINTS_0/WEIGHTS_0 in
the SkinnedMeshRenderer's own bone order, and m_BindPose as the inverse bind
matrices. Rather than re-derive any of that -- which is how a preview ends up
subtly disagreeing with the exported .glb -- this walks the identical path and
hands back arrays.

    import asset_tools.assetlib as A
    from asset_tools import meshdata, render3d

    with A.Bundle("pm0025") as b:
        model = meshdata.load(b)             # bones, meshes, textures
        pic = render3d.render(model, width=520, height=520, yaw=35, pitch=-12)

Design notes, since a preview is where it is tempting to cheat:

* Coordinates stay in Unity's left-handed frame. The glTF exporter does the same,
  so the preview and the export agree, and nothing here has to reason about a
  mirrored model.
* The bind pose is the *correct* thing to show for a texture editor. An animation
  would prove the skin works; it would also make the body squashed and hard to
  paint on. :func:`pose_at` can drive a blend if you want to check a pose.
* Textures are sampled nearest-neighbour with a checkerboard behind them, so
  alpha and a mis-scaled UV are visible instead of hidden.
"""
import numpy as np

from asset_tools import assetlib as A
from asset_tools import unitypy_fix as U


# --------------------------------------------------------------------------
# small matrix helpers (4x4, row-vector convention: v' = v @ M)
# --------------------------------------------------------------------------
def _identity():
    return np.eye(4, dtype=np.float64)


def trs(translation, rotation_quat, scale=(1.0, 1.0, 1.0)):
    """Unity's own T*R*S, with quaternions stored xyzw as Unity stores them.

    Written out rather than borrowed from a library because the composition order
    has to match Unity's or every bone in the preview sits a few degrees off and
    the mesh comes apart in a way that is hard to read as a rotation bug.
    """
    x, y, z, w = [float(v) for v in rotation_quat]
    n = (x * x + y * y + z * z + w * w) ** 0.5
    if n < 1e-12:
        rot = _identity()[:3, :3]
    else:
        x, y, z, w = x / n, y / n, z / n, w / n
        rot = np.array([
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ], dtype=np.float64)
    m = _identity()
    sx, sy, sz = [float(s) for s in scale]
    m[:3, :3] = rot * np.array([sx, sy, sz], dtype=np.float64)
    m[:3, 3] = [float(v) for v in translation]
    return m


def _flat16(m):
    """One Unity 4x4 as 16 floats, from either shape the typetree uses.

    read_typetree() gives [{"e00": ..., "e01": ...}, ...] for a single matrix
    while the parsed object and hand-written constants give 16 plain numbers.
    A *list of* matrices is not handled here -- callers iterate, because quietly
    flattening 43 bind poses into one is how a skin ends up with no bones at all.
    """
    if isinstance(m, dict):
        return [float(m.get("e%02d" % i) or 0.0) for i in range(16)]
    out = []
    for v in np.asarray(m, dtype=np.float64).reshape(-1):
        out.append(float(v))
    return out


def _to_mat4(m):
    """Unity's 16-float m_LocalToWorld / m_BindPose -> 4x4 row-vector."""
    f = _flat16(m)
    if len(f) != 16:
        return _identity()
    # Unity stores columns and the flat order runs down the columns, so reshape
    # already gives the transpose a row-vector multiply wants.
    return np.asarray(f, dtype=np.float64).reshape(4, 4).T.copy()


def _quat_of(mat4):
    """Rotation as xyzw, from a row-vector 4x4 whose basis may carry scale."""
    m = np.array(mat4, dtype=np.float64, copy=True)
    basis = m[:3, :3]
    for i in range(3):                     # strip scale, and shear with it
        n = np.linalg.norm(basis[i])
        if n > 1e-12:
            basis[i] /= n
    t = np.trace(basis)
    if t > 0:
        s = (t + 1.0) ** 0.5 * 2
        w = 0.25 * s
        x = (basis[2, 1] - basis[1, 2]) / s
        y = (basis[0, 2] - basis[2, 0]) / s
        z = (basis[1, 0] - basis[0, 1]) / s
    elif basis[0, 0] > basis[1, 1] and basis[0, 0] > basis[2, 2]:
        s = (1.0 + basis[0, 0] - basis[1, 1] - basis[2, 2]) ** 0.5 * 2
        w = (basis[2, 1] - basis[1, 2]) / s
        x = 0.25 * s
        y = (basis[0, 1] + basis[1, 0]) / s
        z = (basis[0, 2] + basis[2, 0]) / s
    elif basis[1, 1] > basis[2, 2]:
        s = (1.0 + basis[1, 1] - basis[0, 0] - basis[2, 2]) ** 0.5 * 2
        w = (basis[0, 2] - basis[2, 0]) / s
        x = (basis[0, 1] + basis[1, 0]) / s
        y = 0.25 * s
        z = (basis[1, 2] + basis[2, 1]) / s
    else:
        s = (1.0 + basis[2, 2] - basis[0, 0] - basis[1, 1]) ** 0.5 * 2
        w = (basis[1, 0] - basis[0, 1]) / s
        x = (basis[0, 2] + basis[2, 0]) / s
        y = (basis[1, 2] + basis[2, 1]) / s
        z = 0.25 * s
    return (x, y, z, w)


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------
def _pid(ref):
    """A PPtr ({"m_FileID":..,"m_PathID":..}) or a bare int -> path id, 0 if none."""
    if isinstance(ref, dict):
        return int(ref.get("m_PathID") or 0)
    try:
        return int(ref or 0)
    except (TypeError, ValueError):
        return 0


def _transforms(b):
    """path_id -> (local TRS matrix, father path_id) for every Transform."""
    out = {}
    for o in b.env.objects:
        if o.type.name != "Transform":
            continue
        try:
            t = o.read_typetree()
        except Exception:
            continue
        local = trs(A._vec(t, "m_LocalPosition"),
                    A._quat(t, "m_LocalRotation"),
                    A._vec(t, "m_LocalScale", (1.0, 1.0, 1.0)))
        out[o.path_id] = (local,
                          _pid(t.get("m_Father")))
    return out


def _world(transforms, pid, cache=None):
    """Accumulated world matrix for a Transform, walking up m_Father links."""
    if cache is None:
        cache = {}
    if pid in cache:
        return cache[pid]
    hit = transforms.get(pid)
    if hit is None:
        return _identity()
    local, father = hit
    # Iterative, not recursive: a broken father link would otherwise blow the
    # stack, and a corrupted bundle is exactly when someone opens the preview.
    chain, seen = [], set()
    cur = pid
    while cur in transforms and cur not in cache and cur not in seen:
        seen.add(cur)
        chain.append(cur)
        cur = transforms[cur][1]
    base = cache.get(cur, _identity())
    for p in reversed(chain):
        base = transforms[p][0] @ base
        cache[p] = base
    return cache.get(pid, _identity())


def _meshes(b):
    """path_id -> a geometry dict of numpy arrays, from UnityPy's mesh reader."""
    out = {}
    for o in b.env.objects:
        if o.type.name != "Mesh":
            continue
        try:
            d = o.read()
        except Exception:
            continue
        try:
            t = o.read_typetree()
        except Exception:
            t = {}
        if int(t.get("m_MeshCompression", 0)):
            continue                      # these bundles ship uncompressed
        h = U.mesh_handler(d)
        if h is None or not h.m_VertexCount or not h.m_Vertices:
            continue
        pos = np.asarray(h.m_Vertices, dtype=np.float32).reshape(-1, 3)
        nrm = (np.asarray(h.m_Normals, dtype=np.float32).reshape(-1, 3)
               if h.m_Normals else np.zeros_like(pos))
        uv = (np.asarray(h.m_UV0, dtype=np.float32).reshape(-1, 2)
              if h.m_UV0 else np.zeros((len(pos), 2), dtype=np.float32))
        j4 = (np.asarray(h.m_BoneIndices, dtype=np.int32).reshape(-1, 4)
              if h.m_BoneIndices else np.zeros((len(pos), 4), dtype=np.int32))
        w4 = (np.asarray(h.m_BoneWeights, dtype=np.float32).reshape(-1, 4)
              if h.m_BoneWeights else np.zeros((len(pos), 4), dtype=np.float32))
        # get_triangles() returns ONE LIST PER SUBMESH, and that grouping is the
        # whole reason Venusaur's body came out wearing its eye texture: the
        # submeshes were concatenated here, so the renderer's first material was
        # applied to every one of them. Keep the groups, and keep the flattened
        # list too for the callers that just want "all the triangles".
        groups = []
        for group in h.get_triangles():
            if not len(group):
                continue
            arr = np.asarray(group, dtype=np.int32).reshape(-1, 3)
            if len(arr):
                groups.append(arr)
        if not groups:
            continue
        tris = np.concatenate(groups, axis=0)
        out[o.path_id] = {
            "name": d.m_Name,
            "positions": pos,
            "normals": nrm,
            "uvs": uv,
            "joints": j4,
            "weights": w4,
            "tris": tris,
            "submeshes": groups,
            "bind_pose": list(t.get("m_BindPose") or []),
        }
    return out


def _materials(b):
    """renderer path_id -> the texture path_id its first material shows."""
    mats = {}
    for o in b.env.objects:
        if o.type.name != "Material":
            continue
        try:
            mats[o.path_id] = o.read_typetree()
        except Exception:
            continue
    return mats


def _renderer_transforms(b):
    """GameObject path_id -> Transform path_id, from every GameObject.

    A renderer's own transform is what the mesh's vertices are expressed
    relative to. SkinnedMeshRenderer does not carry a direct pointer to it (it
    points at the GameObject, which lists the components), so the link has to be
    walked; getting it wrong silently offsets the model instead of erroring.
    """
    transform_pids = {o.path_id for o in b.env.objects
                      if o.type.name == "Transform"}
    out = {}
    for o in b.env.objects:
        if o.type.name != "GameObject":
            continue
        try:
            t = o.read_typetree()
        except Exception:
            continue
        for comp in (t.get("m_Component") or []):
            pid = _pid(comp)
            if pid in transform_pids:
                out[o.path_id] = pid
                break
    return out


def _submesh_textures(mat_textures, n_submeshes):
    """Line a renderer's per-submesh material textures up with its submeshes.

    Unity allows fewer materials than submeshes only in malformed data, so the
    interesting cases are "one material, many submeshes" (repeat it, which is what
    a single-material mesh means anyway) and "more materials than submeshes" (the
    extras are unused). A submesh whose own material has no _MainTex keeps its
    None, because the renderer draws those with the flat fallback colour instead
    of silently borrowing a neighbour's texture.
    """
    if n_submeshes <= 0:
        return []
    if not mat_textures:
        return [None] * n_submeshes
    if len(mat_textures) >= n_submeshes:
        return list(mat_textures[:n_submeshes])
    return [mat_textures[min(i, len(mat_textures) - 1)]
            for i in range(n_submeshes)]


def load(b, max_meshes=3):
    """Everything render3d needs, read from an open :class:`assetlib.Bundle`.

    `max_meshes` keeps the biggest few: a pm bundle is one body plus shadow and
    effect meshes, and drawing the attack-fx geometry over the body only makes
    the body harder to see.
    """
    transforms = _transforms(b)
    meshes = _meshes(b)
    materials = _materials(b)
    if not meshes:
        raise ValueError("no readable uncompressed mesh in this bundle")

    renderers = [r for r in A._renderers(b) if _pid(r.get("m_Mesh")) in meshes]
    if not renderers:
        raise ValueError("this bundle has no skinned mesh to preview")
    renderers.sort(key=lambda r: len(meshes[
        int(r["m_Mesh"]["m_PathID"])]["positions"]), reverse=True)
    renderers = renderers[:max_meshes]

    # textures as PIL images, keyed by path_id
    textures = {}
    for row in b.textures():
        if not row.get("name") or row.get("broken"):
            continue
        try:
            textures[row["path_id"]] = (b.texture_png(row["path_id"]), row)
        except Exception:
            continue

    world_cache = {}
    go_transforms = _renderer_transforms(b)
    parts = []
    for r in renderers:
        mesh_pid = _pid(r["m_Mesh"])
        g = meshes[mesh_pid]
        bone_pids = [_pid(p) for p in (r.get("m_Bones") or [])]
        inv_bind = [_to_mat4(m) for m in g["bind_pose"]]
        # Only a skin whose bone count matches its bind pose is usable; a short
        # one would index past the end of the matrix list.
        if not bone_pids or len(bone_pids) != len(inv_bind):
            bone_pids, inv_bind = [], []
        bones = [{"path_id": p,
                  "world": _world(transforms, p, world_cache),
                  "inv_bind": inv_bind[i] if i < len(inv_bind) else _identity()}
                 for i, p in enumerate(bone_pids)]
        go_pid = _pid(r.get("m_GameObject"))
        tr_pid = go_transforms.get(go_pid, 0)
        # A renderer carries one material PER SUBMESH, not one material. Taking
        # m_Materials[0] -- as this used to -- paints every submesh with the first
        # material's texture, which is how a Venusaur body ends up wearing its eye
        # texture. So build the list, then line it up with the submeshes.
        mat_pids = [_pid(p) for p in (r.get("m_Materials") or [])]
        subs = g.get("submeshes") or []
        sub_textures = _submesh_textures(
            [A._material_texture(materials, p) for p in mat_pids], len(subs))
        parts.append({
            "name": g["name"],
            # the renderer's own transform, NOT the root bone's
            "root": _world(transforms, tr_pid, world_cache) if tr_pid
            else _identity(),
            "transform_path_id": tr_pid,
            "bones": bones,
            "mesh": g,
            # kept for the glTF exporter and as the fallback when a mesh has no
            # submesh information at all
            "texture": sub_textures[0] if sub_textures else None,
            "submesh_textures": sub_textures,
        })

    return {
        "parts": parts,
        "textures": textures,
        "bone_count": sum(len(p["bones"]) for p in parts),
        "vertex_count": sum(len(p["mesh"]["positions"]) for p in parts),
    }


# --------------------------------------------------------------------------
# skinning
# --------------------------------------------------------------------------
def skin_matrices(bones):
    """Per-bone skinning matrices as a (nbones, 4, 4) stack: W @ IBM.

    Kept for driving an explicit pose -- feeding a blend of animation clips in,
    or checking that a pose deforms the way it should.

    Do NOT use this to draw the bind pose. In the bind pose the skinning matrix
    is the identity, so the mesh's stored vertex buffer is already the correct
    answer; going through the bones only to divide it back out needs the exact
    frame the model was bound in, and pm bundles do not carry that frame in a
    form you can read back. See :func:`posed_positions`.
    """
    if not bones:
        return np.zeros((0, 4, 4), dtype=np.float64)
    return np.stack([b["world"] @ b["inv_bind"] for b in bones])


def posed_positions(part, matrices=None):
    """World-space positions for one part's vertices.

    With no `matrices`, this is the bind pose and it is simply the stored vertex
    buffer moved by the renderer's own transform -- because in the bind pose the
    per-joint skinning matrix is the identity. That is not a shortcut, it is the
    definition, and it is why this preview agrees with the exported .glb instead
    of drifting from it.

    Pass `matrices` (see :func:`skin_matrices`) to draw a different pose.
    """
    g = part["mesh"]
    pos = np.asarray(g["positions"], dtype=np.float64)
    if matrices is not None and len(matrices):
        w = g["weights"].astype(np.float64)
        total = w.sum(axis=1, keepdims=True)
        w = w / np.where(total > 1e-8, total, 1.0)
        empty = (total <= 1e-8).reshape(-1)
        w[empty] = 0.0
        w[empty, 0] = 1.0            # pin unweighted verts instead of dropping
        vh = np.concatenate([pos, np.ones((len(pos), 1))], axis=1)
        # matrices[j] is (nv, 4, 4, 4): four joints per vertex, blended by weight.
        mh = np.einsum("nj,njab,nb->nab", w, matrices[g["joints"]], vh)
        pos = mh[:, :, :3].sum(axis=1)
    root = np.asarray(part["root"], dtype=np.float64)
    vh = np.concatenate([pos, np.ones((len(pos), 1))], axis=1)
    return (vh @ root)[:, :3]


def posed_normals(part, matrices=None):
    """Normals matching :func:`posed_positions`.

    In the bind pose these are just the stored normals under the renderer's
    rotation, because the skinning matrix is the identity. For a driven pose the
    joints' rotation part is blended by the same weights -- linear rather than
    per-triangle renormalised, which is smooth and, for a preview lit by one
    arbitrary key light, indistinguishable.
    """
    g = part["mesh"]
    n = np.asarray(g["normals"], dtype=np.float64)
    if matrices is not None and len(matrices):
        w = g["weights"].astype(np.float64)
        total = w.sum(axis=1, keepdims=True)
        w = w / np.where(total > 1e-8, total, 1.0)
        empty = (total <= 1e-8).reshape(-1)
        w[empty] = 0.0
        w[empty, 0] = 1.0
        rot = matrices[g["joints"]][:, :, :3, :3]
        n = np.einsum("nj,njab,nb->na", w, rot, n)
    root = np.asarray(part["root"], dtype=np.float64)[:3, :3]
    n = n @ root
    length = np.linalg.norm(n, axis=1, keepdims=True)
    return np.where(length > 1e-8, n / np.where(length > 1e-8, length, 1.0), n)
