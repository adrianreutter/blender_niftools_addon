"""This script contains helper methods for texture pathing."""

# ***** BEGIN LICENSE BLOCK *****
#
# Copyright © 2020, NIF File Format Library and Tools contributors.
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
from functools import reduce
import operator
import traceback
import os.path

import bpy
from generated.formats.nif import classes as NifClasses

from io_scene_niftools.utils.singleton import NifOp
from io_scene_niftools.utils.logging import NifLog


class TextureLoader:

    external_textures = set()

    @staticmethod
    def load_image(tex_path):
        """Returns an image or a generated image if none was found"""
        name = os.path.basename(tex_path)
        if name not in bpy.data.images:
            b_image = bpy.data.images.new(name=name, width=1, height=1, alpha=True)
            b_image.filepath=tex_path
        else:
            b_image = bpy.data.images[name]
        return b_image

    def import_texture_source(self, source):
        """Convert a NiSourceTexture block, or simply a path string, to a Blender Texture object.
        :return Texture object
        """

        # if the source block is not linked then return None
        if not source:
            return None

        if isinstance(source, NifClasses.NiSourceTexture) and not source.use_external and NifOp.props.use_embedded_texture:
            return self.import_embedded_texture_source(source)
        else:
            return self.import_external_source(source)

    def import_embedded_texture_source(self, source):
        # first try to use the actual file name of this NiSourceTexture
        tex_name = source.file_name
        tex_path = os.path.join(os.path.dirname(NifOp.props.filepath), tex_name)
        # not set, then use generated sequence name
        if not tex_name:
            tex_path = self.generate_image_name()

        # only save them once per run, obviously only useful if file_name was set
        if tex_path not in self.external_textures:
            # save embedded texture as dds file
            with open(tex_path, "wb") as stream:
                try:
                    NifLog.info(f"Saving embedded texture as {tex_path}")
                    source.pixel_data.save_as_dds(stream)
                except ValueError:
                    NifLog.warn(f"Pixel format not supported in embedded texture {tex_path}!")
                    traceback.print_exc()
            self.external_textures.add(tex_path)

        return self.load_image(tex_path)

    @staticmethod
    def generate_image_name():
        """Find a file name (but avoid overwriting)"""
        n = 0
        while n < 10000:
            fn = f"image{n:0>4d}.dds"
            tex = os.path.join(os.path.dirname(NifOp.props.filepath), fn)
            if not os.path.exists(tex):
                break
            n += 1
        return tex

    def import_external_source(self, source):
        # the texture uses an external image file
        if isinstance(source, NifClasses.NiSourceTexture):
            fn = source.file_name
        elif isinstance(source, str):
            fn = source
        else:
            raise TypeError("source must be NiSourceTexture or str")

        fn = fn.replace('\\', os.sep)
        fn = fn.replace('/', os.sep)
        tex = fn
        # probably not found, but load a dummy regardless
        return self.load_image(tex)
