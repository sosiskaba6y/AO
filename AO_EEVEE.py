bl_info = {
    "name": "Ambient Occlusion",
    "author": "MIKE",
    "version": (1, 1, 0),
    "blender": (4, 2, 0),
    "location": "Properties > Render > Ambient Occlusion",
    "description": "Ambient Occlusion settings like in old EEVEE",
    "category": "Render",
}

import bpy
from bpy.props import BoolProperty, FloatProperty

GROUP_NODE_NAME = "AO_Group"
GROUP_TREE_NAME = "EEVEE AO"
VIEWER_NAME = "AO_Viewer"
EEVEE_ENGINES = {"BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"}


# ---------------------------------------------------------------- compositor tree helpers
def comp_tree_exists(scene):
    if hasattr(scene, "compositing_node_group"):  # Blender 5.0+
        return scene.compositing_node_group is not None
    return bool(scene.use_nodes and scene.node_tree and len(scene.node_tree.nodes) > 0)


def get_comp_tree(scene, create=True):
    """Return compositor node tree (Blender 5.0+ node group or 4.x node_tree)."""
    if hasattr(scene, "compositing_node_group"):  # Blender 5.0+
        tree = scene.compositing_node_group
        if tree is None and create:
            tree = bpy.data.node_groups.new("Compositor Nodes", "CompositorNodeTree")
            tree.interface.new_socket("Image", in_out='OUTPUT', socket_type='NodeSocketColor')
            tree.nodes.new("NodeGroupOutput")
            scene.compositing_node_group = tree
        return tree
    if create and not scene.use_nodes:
        scene.use_nodes = True  # creates Render Layers + Composite by default
    return scene.node_tree


def find_output(tree):
    for n in tree.nodes:
        if n.bl_idname in {"NodeGroupOutput", "CompositorNodeComposite"}:
            return n
    return tree.nodes.new("CompositorNodeComposite")


def find_render_layers(tree, scene, view_layer):
    for n in tree.nodes:
        if n.bl_idname == "CompositorNodeRLayers" and n.layer == view_layer.name \
                and (n.scene is None or n.scene == scene):
            return n
    n = tree.nodes.new("CompositorNodeRLayers")
    n.scene = scene
    n.layer = view_layer.name
    return n


def sock_by_name(sockets, name):
    for s in sockets:
        if s.name == name:
            return s
    return None


def sock_by_identifier(sockets, identifier):
    for s in sockets:
        if s.identifier == identifier:
            return s
    return None


# ---------------------------------------------------------------- AO node group
def new_mix_node(tree):
    try:
        n = tree.nodes.new("ShaderNodeMix")  # Blender 4.5/5.0+ compositor Mix
        n.data_type = 'RGBA'
        n.blend_type = 'MULTIPLY'
        n.clamp_result = False
        n.clamp_factor = True
        return n
    except RuntimeError:
        n = tree.nodes.new("CompositorNodeMixRGB")  # older versions
        n.blend_type = 'MULTIPLY'
        n.use_clamp = False
        return n


def mix_sockets(n):
    """Returns (factor, A, B, result)."""
    if n.bl_idname == "ShaderNodeMix":
        fac = sock_by_identifier(n.inputs, "Factor_Float") or n.inputs[0]
        a = sock_by_identifier(n.inputs, "A_Color")
        b = sock_by_identifier(n.inputs, "B_Color")
        res = sock_by_identifier(n.outputs, "Result_Color")
        return fac, a, b, res
    return n.inputs[0], n.inputs[1], n.inputs[2], n.outputs[0]


def _set(obj, name, value):
    """Set a property if this Blender version has it, otherwise just report."""
    try:
        setattr(obj, name, value)
    except Exception as e:
        print("[EEVEE AO] skipped %s.%s: %s" % (getattr(obj, "bl_idname", obj), name, e))


def _need(sock, what):
    if sock is None:
        raise RuntimeError("[EEVEE AO] socket not found: " + what)
    return sock


def build_ao_group():
    g = bpy.data.node_groups.new(GROUP_TREE_NAME, "CompositorNodeTree")
    try:
        iface = g.interface
        iface.new_socket(name="Image", in_out='INPUT', socket_type='NodeSocketColor')
        iface.new_socket(name="Ambient Occlusion", in_out='INPUT', socket_type='NodeSocketColor')
        iface.new_socket(name="Depth", in_out='INPUT', socket_type='NodeSocketFloat')
        fac_item = iface.new_socket(name="Factor", in_out='INPUT', socket_type='NodeSocketFloat')
        _set(fac_item, "default_value", 1.0)
        _set(fac_item, "min_value", 0.0)
        _set(fac_item, "max_value", 1.0)
        _set(fac_item, "subtype", 'FACTOR')
        iface.new_socket(name="Image", in_out='OUTPUT', socket_type='NodeSocketColor')

        nodes, links = g.nodes, g.links
        gi = nodes.new("NodeGroupInput")
        gi.location = (-500, 0)
        go = nodes.new("NodeGroupOutput")
        go.location = (500, 0)

        defocus = nodes.new("CompositorNodeDefocus")
        defocus.location = (-100, -180)
        _set(defocus, "bokeh", 'CIRCULAR')
        _set(defocus, "angle", 0.0)
        _set(defocus, "f_stop", 128.0)
        _set(defocus, "use_zbuffer", False)
        _set(defocus, "z_scale", 1.0)
        _set(defocus, "blur_max", 16.0)

        mix = new_mix_node(g)
        mix.location = (200, 0)
        fac, a, b, res = mix_sockets(mix)

        df_in = sock_by_name(defocus.inputs, "Image") or defocus.inputs[0]
        df_z = sock_by_name(defocus.inputs, "Z") or defocus.inputs[1]
        df_out = defocus.outputs[0]

        links.new(_need(sock_by_name(gi.outputs, "Image"), "group input Image"), a)
        links.new(_need(sock_by_name(gi.outputs, "Ambient Occlusion"), "group input AO"), df_in)
        links.new(_need(sock_by_name(gi.outputs, "Depth"), "group input Depth"), df_z)
        links.new(df_out, b)
        links.new(_need(sock_by_name(gi.outputs, "Factor"), "group input Factor"), fac)
        links.new(res, go.inputs[0])

        g["eevee_ao_ok"] = True  # marks a fully built group
    except Exception:
        bpy.data.node_groups.remove(g)
        raise
    return g


def get_ao_group():
    """Return the AO node group, (re)building it if missing or half-built."""
    g = bpy.data.node_groups.get(GROUP_TREE_NAME)
    if g is not None:
        if g.bl_idname == "CompositorNodeTree" and g.get("eevee_ao_ok"):
            return g
        g.name = GROUP_TREE_NAME + " (broken)"  # leftover from a failed build
    return build_ao_group()


def get_group_node(tree):
    return tree.nodes.get(GROUP_NODE_NAME) if tree else None


# ---------------------------------------------------------------- update callbacks
def update_factor(self, context):
    tree = get_comp_tree(self, create=False)
    node = get_group_node(tree)
    if node:
        s = sock_by_name(node.inputs, "Factor")
        if s:
            s.default_value = self.eevee_ao_factor


def update_enabled(self, context):
    if self.eevee_ao_enabled:
        enable_ao(self, context.view_layer)
    else:
        disable_ao(self, context.view_layer)


def zpass_key(view_layer):
    return "eevee_ao_zpass_" + view_layer.name


def enable_ao(scene, view_layer):
    view_layer.use_pass_ambient_occlusion = True

    # remember whether the Depth pass was already on, to restore it later
    key = zpass_key(view_layer)
    if key not in scene:
        scene[key] = bool(view_layer.use_pass_z)
    view_layer.use_pass_z = True

    fresh = not comp_tree_exists(scene)
    tree = get_comp_tree(scene)
    if tree is None:
        return
    links = tree.links

    rl = find_render_layers(tree, scene, view_layer)
    view_layer.update()
    rl.update()

    img_out = sock_by_name(rl.outputs, "Image")
    ao_out = sock_by_name(rl.outputs, "Ambient Occlusion")
    depth_out = sock_by_name(rl.outputs, "Depth")
    if img_out is None or ao_out is None or depth_out is None:
        print("[EEVEE AO] Render Layers sockets not found:", [s.name for s in rl.outputs])
        return

    group = get_group_node(tree)
    if group is not None and getattr(group, "node_tree", None) is None:
        tree.nodes.remove(group)  # empty "Missing Data-Block" node from a failed run
        group = None
    if group is None:
        ao_tree = get_ao_group()  # build first: if this fails, no stray node is left
        group = tree.nodes.new("CompositorNodeGroup")
        group.node_tree = ao_tree
        group.name = GROUP_NODE_NAME
        group.label = "Ambient Occlusion"
        group.location = (rl.location.x + 350, rl.location.y)
        sock_by_name(group.inputs, "Factor").default_value = scene.eevee_ao_factor

        res = group.outputs[0]
        # everything that was fed by Render Layers > Image is now fed by the AO group
        targets = [l.to_socket for l in img_out.links]
        for sock in targets:
            links.new(res, sock)

        if fresh:
            if not targets:
                out = find_output(tree)
                links.new(res, out.inputs[0])
            viewer = tree.nodes.new("CompositorNodeViewer")
            viewer.name = VIEWER_NAME
            viewer["eevee_ao_created"] = True
            viewer.location = (group.location.x + 300, group.location.y - 150)
            links.new(res, viewer.inputs[0])

    links.new(img_out, sock_by_name(group.inputs, "Image"))
    links.new(ao_out, sock_by_name(group.inputs, "Ambient Occlusion"))
    links.new(depth_out, sock_by_name(group.inputs, "Depth"))


def disable_ao(scene, view_layer):
    view_layer.use_pass_ambient_occlusion = False

    key = zpass_key(view_layer)
    if key in scene:
        view_layer.use_pass_z = bool(scene[key])
        del scene[key]

    tree = get_comp_tree(scene, create=False)
    if tree is None:
        return
    group = get_group_node(tree)
    if group is None:
        return

    if getattr(group, "node_tree", None) is None or len(group.outputs) == 0:
        tree.nodes.remove(group)  # broken leftover, nothing to restore
        return

    rl = find_render_layers(tree, scene, view_layer)
    img_out = sock_by_name(rl.outputs, "Image")
    res = group.outputs[0]

    # give the original connections back to Render Layers > Image
    targets = [l.to_socket for l in res.links]
    viewer = tree.nodes.get(VIEWER_NAME)
    if viewer and viewer.get("eevee_ao_created") and viewer.inputs[0].is_linked \
            and viewer.inputs[0].links[0].from_node == group:
        targets = [t for t in targets if t.node != viewer]
        tree.nodes.remove(viewer)
    tree.nodes.remove(group)
    if img_out:
        for sock in targets:
            tree.links.new(img_out, sock)


# ---------------------------------------------------------------- panel
class RENDER_PT_eevee_ao(bpy.types.Panel):
    bl_label = "Ambient Occlusion"
    bl_idname = "RENDER_PT_eevee_ao_addon"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "render"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return context.engine in EEVEE_ENGINES

    def draw_header(self, context):
        self.layout.prop(context.scene, "eevee_ao_enabled", text="")

    def draw(self, context):
        scene = context.scene
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.active = scene.eevee_ao_enabled

        layout.prop(scene, "eevee_ao_factor", text="Factor")

        eevee = context.view_layer.eevee
        if hasattr(eevee, "ambient_occlusion_distance"):
            layout.prop(eevee, "ambient_occlusion_distance", text="Occlusion Distance")


# ---------------------------------------------------------------- register
classes = (RENDER_PT_eevee_ao,)


def register():
    bpy.types.Scene.eevee_ao_enabled = BoolProperty(
        name="Ambient Occlusion",
        description="Enable AO + Depth passes and add the AO group to the compositor",
        default=False,
        update=update_enabled,
    )
    bpy.types.Scene.eevee_ao_factor = FloatProperty(
        name="Factor",
        description="Mix factor of the AO Multiply node",
        default=1.0, min=0.0, max=1.0,
        update=update_factor,
    )
    for c in classes:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    del bpy.types.Scene.eevee_ao_factor
    del bpy.types.Scene.eevee_ao_enabled


if __name__ == "__main__":
    register()
