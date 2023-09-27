"""This module contains helper methods to export Mesh information."""
# ***** BEGIN LICENSE BLOCK *****
#
# Copyright © 2019, NIF File Format Library and Tools contributors.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#    * Redistributions of source code must retain the above copyright
#      notice, this list of conditions and the following disclaimer.
#
#    * Redistributions in binary form must reproduce the above
#      copyright notice, this list of conditions and the following
#      disclaimer in the documentation and/or other materials provided
#      with the distribution.
#
#    * Neither the name of the NIF File Format Library and Tools
#      project nor the names of its contributors may be used to endorse
#      or promote products derived from this software without specific
#      prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
# ***** END LICENSE BLOCK *****

import bpy
import bmesh
import mathutils
import numpy as np
import struct

from generated.formats.nif import classes as NifClasses

import io_scene_niftools.utils.logging
from io_scene_niftools.modules.nif_export.geometry import mesh
from io_scene_niftools.modules.nif_export.animation.morph import MorphAnimation
from io_scene_niftools.modules.nif_export.block_registry import block_store
from io_scene_niftools.modules.nif_export.property.object import ObjectProperty
from io_scene_niftools.modules.nif_export.property.texture.types.nitextureprop import NiTextureProp
from io_scene_niftools.utils import math
from io_scene_niftools.utils.singleton import NifOp, NifData
from io_scene_niftools.utils.logging import NifLog, NifError
from io_scene_niftools.modules.nif_export.geometry.mesh.skin_partition import update_skin_partition


class Mesh:

    def __init__(self):
        self.texture_helper = NiTextureProp.get()
        self.object_property = ObjectProperty()
        self.morph_anim = MorphAnimation()

    def export_tri_shapes(self, b_obj, n_parent, n_root, trishape_name=None):
        """
        Export a blender object ob of the type mesh, child of nif block
        n_parent, as NiTriShape and NiTriShapeData blocks, possibly
        along with some NiTexturingProperty, NiSourceTexture,
        NiMaterialProperty, and NiAlphaProperty blocks. We export one
        n_geom block per mesh material. We also export vertex weights.

        The parameter trishape_name passes on the name for meshes that
        should be exported as a single mesh.
        """
        NifLog.info(f"Exporting {b_obj}")

        assert (b_obj.type == 'MESH')

        # get mesh from b_obj, and evaluate the mesh with modifiers applied, too
        b_mesh = b_obj.data
        eval_mesh = self.get_triangulated_mesh(b_obj)
        eval_mesh.calc_normals_split()

        # getVertsFromGroup fails if the mesh has no vertices
        # (this happens when checking for fallout 3 body parts)
        # so quickly catch this (rare!) case
        if not eval_mesh.vertices:
            # do not export anything
            NifLog.warn(f"{b_obj} has no vertices, skipped.")
            return

        # get the mesh's materials, this updates the mesh material list
        if not isinstance(n_parent, NifClasses.RootCollisionNode):
            mesh_materials = eval_mesh.materials
        else:
            # ignore materials on collision trishapes
            mesh_materials = []

        # if mesh has no materials, all face material indices should be 0, so fake one material in the material list
        if not mesh_materials:
            mesh_materials = [None]

        # vertex color check
        mesh_hasvcol = eval_mesh.vertex_colors
        # list of body part (name, index, vertices) in this mesh
        polygon_parts = self.get_polygon_parts(b_obj, eval_mesh)
        game = bpy.context.scene.niftools_scene.game

        # Non-textured materials, vertex colors are used to color the mesh
        # Textured materials, they represent lighting details

        # let's now export one n_geom for every mesh material
        # TODO [material] needs refactoring - move material, texture, etc. to separate function
        for b_mat_index, b_mat in enumerate(mesh_materials):

            mesh_hasnormals = False
            if b_mat is not None:
                mesh_hasnormals = True  # for proper lighting
                if (game == 'SKYRIM') and b_mat.niftools_shader.model_space_normals:
                    mesh_hasnormals = False  # for proper lighting

            # create a n_geom block
            if not NifOp.props.stripify:
                n_geom = block_store.create_block("NiTriShape", b_obj)
                n_geom.data = block_store.create_block("NiTriShapeData", b_obj)
            else:
                n_geom = block_store.create_block("NiTriStrips", b_obj)
                n_geom.data = block_store.create_block("NiTriStripsData", b_obj)

            # fill in the NiTriShape's non-trivial values
            if isinstance(n_parent, NifClasses.RootCollisionNode):
                n_geom.name = ""
            else:
                if not trishape_name:
                    if n_parent.name:
                        n_geom.name = "Tri " + n_parent.name
                    else:
                        n_geom.name = "Tri " + b_obj.name
                else:
                    n_geom.name = trishape_name

                # multimaterial meshes: add material index (Morrowind's child naming convention)
                if len(mesh_materials) > 1:
                    n_geom.name = f"{n_geom.name}: {b_mat_index}"
                else:
                    n_geom.name = block_store.get_full_name(n_geom)

            self.set_mesh_flags(b_obj, n_geom)

            # extra shader for Sid Meier's Railroads
            if game == 'SID_MEIER_S_RAILROADS':
                n_geom.has_shader = True
                n_geom.shader_name = "RRT_NormalMap_Spec_Env_CubeLight"
                n_geom.unknown_integer = -1  # default

            # if we have an animation of a blender mesh
            # an intermediate NiNode has been created which holds this b_obj's transform
            # the n_geom itself then needs identity transform (default)
            if trishape_name is not None:
                # only export the bind matrix on trishapes that were not animated
                math.set_object_matrix(b_obj, n_geom)

            # check if there is a parent
            if n_parent:
                # add texture effect block (must be added as parent of the n_geom)
                n_parent = self.export_texture_effect(n_parent, b_mat)
                # refer to this mesh in the parent's children list
                n_parent.add_child(n_geom)

            self.object_property.export_properties(b_obj, b_mat, n_geom)

            # -> now comes the real export

            '''
                NIF has one uv vertex and one normal per vertex,
                per vert, vertex coloring.

                NIF uses the normal table for lighting.
                Smooth faces should use Blender's vertex normals,
                solid faces should use Blender's face normals.

                Blender's uv vertices and normals per face.
                Blender supports per face vertex coloring,
            '''

            # We now extract vertices, uv-vertices, normals, and
            # vertex colors from the mesh's face list. Some vertices must be duplicated.

            # The following algorithm extracts all unique quads(vert, uv-vert, normal, vcol),
            # produce lists of vertices, uv-vertices, normals, vertex colors, and face indices.

            b_uv_layers = eval_mesh.uv_layers
            vertquad_list = []  # (vertex, uv coordinate, normal, vertex color) list
            vertex_map = [None for _ in range(len(eval_mesh.vertices))]  # blender vertex -> nif vertices
            vertex_positions = []
            normals = []
            vertex_colors = []
            uv_coords = []
            triangles = []
            # for each face in triangles, a body part index
            bodypartfacemap = []
            polygons_without_bodypart = []

            if eval_mesh.polygons:
                if b_uv_layers:
                    # if we have uv coordinates double check that we have uv data
                    if not eval_mesh.uv_layer_stencil:
                        NifLog.warn(f"No UV map for texture associated with selected mesh '{eval_mesh.name}'.")

            use_tangents = False
            if b_uv_layers and mesh_hasnormals:
                if game in ('OBLIVION', 'FALLOUT_3', 'SKYRIM') or (game in self.texture_helper.USED_EXTRA_SHADER_TEXTURES):
                    use_tangents = True
                    eval_mesh.calc_tangents(uvmap=b_uv_layers[0].name)
                    tangents = []
                    bitangent_signs = []

            if game in ('FALLOUT_3', 'SKYRIM'):
                if len(b_uv_layers) > 1:
                    raise NifError(f"{game} does not support multiple UV layers.")

            for poly in eval_mesh.polygons:

                # does the face belong to this n_geom?
                if b_mat is not None and poly.material_index != b_mat_index:
                    # we have a material but this face has another material, so skip
                    continue

                f_numverts = len(poly.vertices)
                if f_numverts < 3:
                    continue  # ignore degenerate polygons
                assert ((f_numverts == 3) or (f_numverts == 4))  # debug

                # find (vert, uv-vert, normal, vcol) quad, and if not found, create it
                f_index = [-1] * f_numverts
                for i, loop_index in enumerate(poly.loop_indices):

                    fv_index = eval_mesh.loops[loop_index].vertex_index
                    vertex = eval_mesh.vertices[fv_index]
                    vertex_index = vertex.index
                    fv = vertex.co

                    # smooth = vertex normal, non-smooth = face normal)
                    if mesh_hasnormals:
                        if poly.use_smooth:
                            fn = eval_mesh.loops[loop_index].normal
                        else:
                            fn = poly.normal
                    else:
                        fn = None

                    fuv = [uv_layer.data[loop_index].uv for uv_layer in eval_mesh.uv_layers]

                    # TODO [geometry][mesh] Need to map b_verts -> n_verts
                    if mesh_hasvcol:
                        f_col = list(eval_mesh.vertex_colors[0].data[loop_index].color)
                    else:
                        f_col = None

                    vertquad = (fv, fuv, fn, f_col)

                    # check for duplicate vertquad?
                    f_index[i] = len(vertquad_list)
                    if vertex_map[vertex_index] is not None:
                        # iterate only over vertices with the same vertex index
                        for j in vertex_map[vertex_index]:
                            # check if they have the same uvs, normals and colors
                            if self.is_new_face_corner_data(vertquad, vertquad_list[j]):
                                continue
                            # all tests passed: so yes, we already have a vert with the same face corner data!
                            f_index[i] = j
                            break

                    if f_index[i] > 65535:
                        raise NifError("Too many vertices. Decimate your mesh and try again.")

                    if f_index[i] == len(vertquad_list):
                        # first: add it to the vertex map
                        if not vertex_map[vertex_index]:
                            vertex_map[vertex_index] = []
                        vertex_map[vertex_index].append(len(vertquad_list))
                        # new (vert, uv-vert, normal, vcol) quad: add it
                        vertquad_list.append(vertquad)

                        # add the vertex
                        vertex_positions.append(vertquad[0])
                        if mesh_hasnormals:
                            normals.append(vertquad[2])
                        if use_tangents:
                            tangents.append(eval_mesh.loops[loop_index].tangent)
                            bitangent_signs.append([eval_mesh.loops[loop_index].bitangent_sign])
                        if mesh_hasvcol:
                            vertex_colors.append(vertquad[3])
                        if b_uv_layers:
                            uv_coords.append(vertquad[1])

                # now add the (hopefully, convex) face, in triangles
                for i in range(f_numverts - 2):
                    if (b_obj.scale.x + b_obj.scale.y + b_obj.scale.z) > 0:
                        f_indexed = (f_index[0], f_index[1 + i], f_index[2 + i])
                    else:
                        f_indexed = (f_index[0], f_index[2 + i], f_index[1 + i])
                    triangles.append(f_indexed)

                    # add body part number
                    if game not in ('FALLOUT_3', 'SKYRIM') or not polygon_parts:
                        # TODO: or not self.EXPORT_FO3_BODYPARTS):
                        bodypartfacemap.append(0)
                    else:
                        # add the polygon's body part
                        part_index = polygon_parts[poly.index]
                        if part_index >= 0:
                            bodypartfacemap.append(part_index)
                        else:
                            # this signals an error
                            polygons_without_bodypart.append(poly)

            # check that there are no missing body part polygons
            if polygons_without_bodypart:
                self.select_unassigned_polygons(eval_mesh, b_obj, polygons_without_bodypart)

            if len(triangles) > 65535:
                raise NifError("Too many polygons. Decimate your mesh and try again.")
            if len(vertex_positions) == 0:
                continue  # m_4444x: skip 'empty' material indices

            self.set_geom_data(n_geom, vertex_positions, normals, vertex_colors, uv_coords, b_uv_layers)

            # set triangles stitch strips for civ4
            n_geom.data.set_triangles(triangles, stitchstrips=NifOp.props.stitch_strips)

            # update tangent space
            # for extra shader texture games, only export it if those textures are actually exported
            # (civ4 seems to be consistent with not using tangent space on non shadered nifs)
            if use_tangents:
                if game == 'SKYRIM':
                    n_geom.data.bs_data_flags.has_tangents = True
                # calculate the bitangents using the normals, tangent list and bitangent sign
                bitangents = bitangent_signs * np.cross(normals, tangents)
                # B_tan: +d(B_u), B_bit: +d(B_v) and N_tan: +d(N_v), N_bit: +d(N_u)
                # moreover, N_v = 1 - B_v, so d(B_v) = - d(N_v), therefore N_tan = -B_bit and N_bit = B_tan
                self.add_defined_tangents(n_geom,
                                          tangents=-bitangents,
                                          bitangents=tangents,
                                          as_extra_data=(game == 'OBLIVION'))  # as binary extra data only for Oblivion

            # todo [mesh/object] use more sophisticated armature finding, also taking armature modifier into account
            # now export the vertex weights, if there are any
            if b_obj.parent and b_obj.parent.type == 'ARMATURE':
                b_obj_armature = b_obj.parent
                vertgroups = {vertex_group.name for vertex_group in b_obj.vertex_groups}
                bone_names = set(b_obj_armature.data.bones.keys())
                # the vertgroups that correspond to bone_names are bones that influence the mesh
                boneinfluences = vertgroups & bone_names
                if boneinfluences:  # yes we have skinning!
                    # create new skinning instance block and link it
                    skininst, skindata = self.create_skin_inst_data(b_obj, b_obj_armature, polygon_parts)
                    n_geom.skin_instance = skininst

                    # Vertex weights,  find weights and normalization factors
                    vert_list = {}
                    vert_norm = {}
                    unweighted_vertices = []

                    for bone_group in boneinfluences:
                        b_list_weight = []
                        b_vert_group = b_obj.vertex_groups[bone_group]

                        for b_vert in eval_mesh.vertices:
                            if len(b_vert.groups) == 0:  # check vert has weight_groups
                                unweighted_vertices.append(b_vert.index)
                                continue

                            for g in b_vert.groups:
                                if b_vert_group.name in boneinfluences:
                                    if g.group == b_vert_group.index:
                                        b_list_weight.append((b_vert.index, g.weight))
                                        break

                        vert_list[bone_group] = b_list_weight

                        # create normalisation groupings
                        for v in vert_list[bone_group]:
                            if v[0] in vert_norm:
                                vert_norm[v[0]] += v[1]
                            else:
                                vert_norm[v[0]] = v[1]

                    self.select_unweighted_vertices(b_obj, unweighted_vertices)

                    # for each bone, get the vertex weights and add its n_node to the NiSkinData
                    for b_bone_name in boneinfluences:
                        # find vertex weights
                        vert_weights = {}
                        for v in vert_list[b_bone_name]:
                            # v[0] is the original vertex index
                            # v[1] is the weight

                            # vertex_map[v[0]] is the set of vertices (indices) to which v[0] was mapped
                            # so we simply export the same weight as the original vertex for each new vertex

                            # write the weights
                            # extra check for multi material meshes
                            if vertex_map[v[0]] and vert_norm[v[0]]:
                                for vert_index in vertex_map[v[0]]:
                                    vert_weights[vert_index] = v[1] / vert_norm[v[0]]
                        # add bone as influence, but only if there were actually any vertices influenced by the bone
                        if vert_weights:
                            # find bone in exported blocks
                            n_node = self.get_bone_block(b_obj_armature.data.bones[b_bone_name])
                            n_geom.add_bone(n_node, vert_weights)
                    del vert_weights

                    # update bind position skinning data
                    # n_geom.update_bind_position()
                    # override pyffi n_geom.update_bind_position with custom one that is relative to the nif root
                    self.update_bind_position(n_geom, n_root, b_obj_armature)

                    # calculate center and radius for each skin bone data block
                    n_geom.update_skin_center_radius()

                    self.export_skin_partition(b_obj, bodypartfacemap, triangles, n_geom)

            # fix data consistency type
            n_geom.data.consistency_flags = NifClasses.ConsistencyType[b_obj.niftools.consistency_flags]

            # export EGM or NiGeomMorpherController animation
            # shape keys are only present on the raw, unevaluated mesh
            self.morph_anim.export_morph(b_mesh, n_geom, vertex_map)
        return n_geom

    def set_geom_data(self, n_geom, vertex_positions, normals, vertex_colors, uv_coords, b_uv_layers):
        """Sets flat lists of per-vertex data to n_geom"""
        # coords
        n_geom.data.num_vertices = len(vertex_positions)
        n_geom.data.has_vertices = True
        n_geom.data.reset_field("vertices")
        for n_v, b_v in zip(n_geom.data.vertices, vertex_positions):
            n_v.x, n_v.y, n_v.z = b_v
        n_geom.data.update_center_radius()
        # normals
        n_geom.data.has_normals = bool(normals)
        n_geom.data.reset_field("normals")
        for n_v, b_v in zip(n_geom.data.normals, normals):
            n_v.x, n_v.y, n_v.z = b_v
        # vertex_colors
        n_geom.data.has_vertex_colors = bool(vertex_colors)
        n_geom.data.reset_field("vertex_colors")
        for n_v, b_v in zip(n_geom.data.vertex_colors, vertex_colors):
            n_v.r, n_v.g, n_v.b, n_v.a = b_v
        # uv_sets
        if bpy.context.scene.niftools_scene.nif_version == 0x14020007 and bpy.context.scene.niftools_scene.user_version_2:
            data_flags = n_geom.data.bs_data_flags
        else:
            data_flags = n_geom.data.data_flags
        data_flags.has_uv = bool(b_uv_layers)
        if hasattr(data_flags, "num_uv_sets"):
            data_flags.num_uv_sets = len(b_uv_layers)
        else:
            if len(b_uv_layers) > 1:
                NifLog.warn(f"More than one UV layers for game that doesn't support it, only using first UV layer")
        n_geom.data.reset_field("uv_sets")
        for j, n_uv_set in enumerate(n_geom.data.uv_sets):
            for i, n_uv in enumerate(n_uv_set):
                if len(uv_coords[i]) == 0:
                    continue  # skip non-uv textures
                n_uv.u = uv_coords[i][j][0]
                # NIF flips the texture V-coordinate (OpenGL standard)
                n_uv.v = 1.0 - uv_coords[i][j][1]  # opengl standard

    def export_skin_partition(self, b_obj, bodypartfacemap, triangles, n_geom):
        """Attaches a skin partition to n_geom if needed"""
        game = bpy.context.scene.niftools_scene.game
        if NifData.data.version >= 0x04020100 and NifOp.props.skin_partition:
            NifLog.info("Creating skin partition")

            # warn on bad config settings
            if game == 'OBLIVION':
                if NifOp.props.pad_bones:
                    NifLog.warn(
                        "Using padbones on Oblivion export. Disable the pad bones option to get higher quality skin partitions.")

            # Skyrim Special Edition has a limit of 80 bones per partition, but export is not yet supported
            bones_per_partition_lut = {"OBLIVION": 18, "FALLOUT_3": 18, "SKYRIM": 24}
            rec_bones = bones_per_partition_lut.get(game, None)
            if rec_bones is not None:
                if NifOp.props.max_bones_per_partition < rec_bones:
                    NifLog.warn(f"Using less than {rec_bones} bones per partition on {game} export."
                                f"Set it to {rec_bones} to get higher quality skin partitions.")
                elif NifOp.props.max_bones_per_partition > rec_bones:
                    NifLog.warn(f"Using more than {rec_bones} bones per partition on {game} export."
                                f"This may cause issues in-game.")

            part_order = [NifClasses.BSDismemberBodyPartType[face_map.name] for face_map in
                          b_obj.face_maps if face_map.name in NifClasses.BSDismemberBodyPartType.__members__]
            # override pyffi n_geom.update_skin_partition with custom one (that allows ordering)
            n_geom.update_skin_partition = update_skin_partition.__get__(n_geom)
            lostweight = n_geom.update_skin_partition(
                maxbonesperpartition=NifOp.props.max_bones_per_partition,
                maxbonespervertex=NifOp.props.max_bones_per_vertex,
                stripify=NifOp.props.stripify,
                stitchstrips=NifOp.props.stitch_strips,
                padbones=NifOp.props.pad_bones,
                triangles=triangles,
                trianglepartmap=bodypartfacemap,
                maximize_bone_sharing=(game in ('FALLOUT_3', 'SKYRIM')),
                part_sort_order=part_order)

            if lostweight > NifOp.props.epsilon:
                NifLog.warn(
                    f"Lost {lostweight:f} in vertex weights while creating a skin partition for Blender object '{b_obj.name}' (nif block '{n_geom.name}')")

    def update_bind_position(self, n_geom, n_root, b_obj_armature):
        """Transfer the Blender bind position to the nif bind position.
        Sets the NiSkinData overall transform to the inverse of the geometry transform
        relative to the skeleton root, and sets the NiSkinData of each bone to
        the inverse of the transpose of the bone transform relative to the skeleton root, corrected
        for the overall transform."""
        if not n_geom.is_skin():
            return

        # validate skin and set up quick links
        n_geom._validate_skin()
        skininst = n_geom.skin_instance
        skindata = skininst.data
        skelroot = skininst.skeleton_root

        # calculate overall offset (including the skeleton root transform) and use its inverse
        geomtransform = (n_geom.get_transform(skelroot) * skelroot.get_transform()).get_inverse(fast=False)
        skindata.set_transform(geomtransform)

        # for some nifs, somehow n_root is not set properly?!
        if not n_root:
            NifLog.warn(f"n_root was not set, bug")
            n_root = skelroot

        old_position = b_obj_armature.data.pose_position
        b_obj_armature.data.pose_position = 'POSE'

        # calculate bone offsets
        for i, bone in enumerate(skininst.bones):
            bone_name = block_store.block_to_obj[bone].name
            pose_bone = b_obj_armature.pose.bones[bone_name]
            n_bind = math.mathutils_to_nifformat_matrix(math.blender_bind_to_nif_bind(pose_bone.matrix))
            # todo [armature] figure out the correct transform that works universally
            # inverse skin bind in nif armature space, relative to root / geom??
            skindata.bone_list[i].set_transform((n_bind * geomtransform).get_inverse(fast=False))
            # this seems to be correct for skyrim heads, but breaks stuff like ZT2 elephant
            # skindata.bone_list[i].set_transform(bone.get_transform(n_root).get_inverse())

        b_obj_armature.data.pose_position = old_position

    def get_bone_block(self, b_bone):
        """For a blender bone, return the corresponding nif node from the blocks that have already been exported"""
        for n_block, b_obj in block_store.block_to_obj.items():
            if isinstance(n_block, NifClasses.NiNode) and b_bone == b_obj:
                return n_block
        raise NifError(f"Bone '{b_bone.name}' not found.")

    def get_polygon_parts(self, b_obj, b_mesh):
        """Returns the body part indices of the mesh polygons. -1 is either not assigned to a face map or not a valid
        body part"""
        index_group_map = {-1: -1}
        for bodypartgroupname in [member.name for member in NifClasses.BSDismemberBodyPartType]:
            face_map = b_obj.face_maps.get(bodypartgroupname)
            if face_map:
                index_group_map[face_map.index] = NifClasses.BSDismemberBodyPartType[bodypartgroupname]
        if len(index_group_map) <= 1:
            # there were no valid face maps
            return []
        bm = bmesh.new()
        bm.from_mesh(b_mesh)
        bm.faces.ensure_lookup_table()
        fm = bm.faces.layers.face_map.verify()
        polygon_parts = [index_group_map.get(face[fm], -1) for face in bm.faces]
        bm.free()
        return polygon_parts

    def create_skin_inst_data(self, b_obj, b_obj_armature, polygon_parts):
        if bpy.context.scene.niftools_scene.game in ('FALLOUT_3', 'SKYRIM') and polygon_parts:
            skininst = block_store.create_block("BSDismemberSkinInstance", b_obj)
        else:
            skininst = block_store.create_block("NiSkinInstance", b_obj)

        # get skeleton root from custom property
        if b_obj.niftools.skeleton_root:
            n_root_name = b_obj.niftools.skeleton_root
        # or use the armature name
        else:
            n_root_name = block_store.get_full_name(b_obj_armature)
        # make sure that such a block exists, find it
        for block in block_store.block_to_obj:
            if isinstance(block, NifClasses.NiNode):
                if block.name == n_root_name:
                    skininst.skeleton_root = block
                    break
        else:
            raise NifError(f"Skeleton root '{n_root_name}' not found.")

        # create skinning data and link it
        skindata = block_store.create_block("NiSkinData", b_obj)
        skininst.data = skindata

        skindata.has_vertex_weights = True
        # fix geometry rest pose: transform relative to skeleton root
        skindata.set_transform(math.get_object_matrix(b_obj).get_inverse())
        return skininst, skindata

    # TODO [object][flags] Move up to object
    def set_mesh_flags(self, b_obj, trishape):
        # use blender flags
        if (b_obj.type == 'MESH') and (b_obj.niftools.flags != 0):
            trishape.flags = b_obj.niftools.flags
        # fall back to defaults
        else:
            if bpy.context.scene.niftools_scene.game in ('OBLIVION', 'FALLOUT_3', 'SKYRIM'):
                trishape.flags = 0x000E

            elif bpy.context.scene.niftools_scene.game in ('SID_MEIER_S_RAILROADS', 'CIVILIZATION_IV'):
                trishape.flags = 0x0010
            elif bpy.context.scene.niftools_scene.game in ('EMPIRE_EARTH_II',):
                trishape.flags = 0x0016
            elif bpy.context.scene.niftools_scene.game in ('DIVINITY_2',):
                if trishape.name.lower[-3:] in ("med", "low"):
                    trishape.flags = 0x0014
                else:
                    trishape.flags = 0x0016
            else:
                # morrowind
                if b_obj.display_type != 'WIRE':  # not wire
                    trishape.flags = 0x0004  # use triangles as bounding box
                else:
                    trishape.flags = 0x0005  # use triangles as bounding box + hide

    # todo [mesh] join code paths for those two?
    def select_unweighted_vertices(self, b_obj, unweighted_vertices):
        # vertices must be assigned at least one vertex group lets be nice and display them for the user
        if len(unweighted_vertices) > 0:
            for b_scene_obj in bpy.context.scene.objects:
                b_scene_obj.select_set(False)

            bpy.context.view_layer.objects.active = b_obj

            # switch to edit mode to deselect everything in the mesh (not missing vertices or edges)
            bpy.ops.object.mode_set(mode='EDIT', toggle=False)
            bpy.context.tool_settings.mesh_select_mode = (True, False, False)
            bpy.ops.mesh.select_all(action='DESELECT')

            # select unweighted vertices - switch back to object mode to make per-vertex selection
            bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
            for vert_index in unweighted_vertices:
                b_obj.data.vertices[vert_index].select = True

            # switch back to edit mode to make the selection visible and raise exception
            bpy.ops.object.mode_set(mode='EDIT', toggle=False)

            raise NifError("Cannot export mesh with unweighted vertices. "
                           "The unweighted vertices have been selected in the mesh so they can easily be identified.")

    def select_unassigned_polygons(self, b_mesh, b_obj, polygons_without_bodypart):
        """Select any faces which are not weighted to a vertex group"""
        ngon_mesh = b_obj.data
        # make vertex: poly map of the untriangulated mesh
        vert_poly_dict = {i: set() for i in range(len(ngon_mesh.vertices))}
        for face in ngon_mesh.polygons:
            for vertex in face.vertices:
                vert_poly_dict[vertex].add(face.index)

        # translate the tris of polygons_without_bodypart to polygons (assuming vertex order does not change)
        ngons_without_bodypart = []
        for face in polygons_without_bodypart:
            poly_set = vert_poly_dict[face.vertices[0]]
            for vertex in face.vertices[1:]:
                poly_set = poly_set.intersection(vert_poly_dict[vertex])
                if len(poly_set) == 0:
                    break
            else:
                for poly in poly_set:
                    ngons_without_bodypart.append(poly)

        # switch to object mode so (de)selecting faces works
        bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
        # select mesh object
        for b_deselect_obj in bpy.context.scene.objects:
            b_deselect_obj.select_set(False)
        bpy.context.view_layer.objects.active = b_obj
        # switch to edit mode to deselect everything in the mesh (not missing vertices or edges)
        bpy.ops.object.mode_set(mode='EDIT', toggle=False)
        bpy.context.tool_settings.mesh_select_mode = (False, False, True)
        bpy.ops.mesh.select_all(action='DESELECT')

        # switch back to object mode to make per-face selection
        bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
        for poly in ngons_without_bodypart:
            ngon_mesh.polygons[poly].select = True

        # select bad polygons switch to edit mode to select polygons
        bpy.ops.object.mode_set(mode='EDIT', toggle=False)

        # raise exception
        raise NifError(f"Some polygons of {b_obj.name} not assigned to any body part."
                       f"The unassigned polygons have been selected in the mesh so they can easily be identified.")

    def is_new_face_corner_data(self, vertquad, v_quad_old):
        """Compares vert info to old vert info if relevant data is present"""
        # uvs
        if v_quad_old[1]:
            for i in range(2):
                if max(abs(vertquad[1][uv_index][i] - v_quad_old[1][uv_index][i]) for uv_index in
                       range(len(v_quad_old[1]))) > NifOp.props.epsilon:
                    return True
        # normals
        if v_quad_old[2]:
            for i in range(3):
                if abs(vertquad[2][i] - v_quad_old[2][i]) > NifOp.props.epsilon:
                    return True
        # vcols
        if v_quad_old[3]:
            for i in range(4):
                if abs(vertquad[3][i] - v_quad_old[3][i]) > NifOp.props.epsilon:
                    return True

    def export_texture_effect(self, n_block, b_mat):
        # todo [texture] detect effect
        ref_mtex = False
        if ref_mtex:
            # create a new parent block for this shape
            extra_node = block_store.create_block("NiNode", ref_mtex)
            n_block.add_child(extra_node)
            # set default values for this ninode
            extra_node.rotation.set_identity()
            extra_node.scale = 1.0
            extra_node.flags = 0x000C  # morrowind
            # create texture effect block and parent the texture effect and n_geom to it
            texeff = self.texture_helper.export_texture_effect(ref_mtex)
            extra_node.add_child(texeff)
            extra_node.add_effect(texeff)
            return extra_node
        return n_block

    def get_triangulated_mesh(self, b_obj):
        # TODO [geometry][mesh] triangulation could also be done using loop_triangles, without a modifier
        # get the armature influencing this mesh, if it exists
        b_armature_obj = b_obj.find_armature()
        if b_armature_obj:
            old_position = b_armature_obj.data.pose_position
            b_armature_obj.data.pose_position = 'REST'

        # make sure the model has a triangulation modifier
        self.ensure_tri_modifier(b_obj)

        # make a copy with all modifiers applied
        dg = bpy.context.evaluated_depsgraph_get()
        eval_obj = b_obj.evaluated_get(dg)
        eval_mesh = eval_obj.to_mesh(preserve_all_data_layers=True, depsgraph=dg)
        if b_armature_obj:
            b_armature_obj.data.pose_position = old_position
        return eval_mesh

    def ensure_tri_modifier(self, b_obj):
        """Makes sure that ob has a triangulation modifier in its stack."""
        for mod in b_obj.modifiers:
            if mod.type in ('TRIANGULATE',):
                break
        else:
            b_obj.modifiers.new('Triangulate', 'TRIANGULATE')

    def add_defined_tangents(self, n_geom, tangents, bitangents, as_extra_data):
        # check if size of tangents and bitangents is equal to num_vertices
        if not (len(tangents) == len(bitangents) == n_geom.data.num_vertices):
            raise NifError(f'Number of tangents or bitangents does not agree with number of vertices in {n_geom.name}')

        if as_extra_data:
            # if tangent space extra data already exists, use it
            # find possible extra data block
            extra_name = 'Tangent space (binormal & tangent vectors)'
            for extra in n_geom.get_extra_datas():
                if isinstance(extra, NifClasses.NiBinaryExtraData):
                    if extra.name == extra_name:
                        break
            else:
                # create a new block and link it
                extra = NifClasses.NiBinaryExtraData(NifData.data)
                extra.name = extra_name
                n_geom.add_extra_data(extra)
            # write the data
            extra.binary_data = np.concatenate((tangents, bitangents), axis=0).astype('<f').tobytes()
        else:
            # set tangent space flag
            n_geom.data.extra_vectors_flags = 16
            # XXX used to be 61440
            # XXX from Sid Meier's Railroad
            n_geom.data.reset_field("tangents")
            for n_v, b_v in zip(n_geom.data.tangents, tangents):
                n_v.x, n_v.y, n_v.z = b_v
            n_geom.data.reset_field("bitangents")
            for n_v, b_v in zip(n_geom.data.bitangents, bitangents):
                n_v.x, n_v.y, n_v.z = b_v
