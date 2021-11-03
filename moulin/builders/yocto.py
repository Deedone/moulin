# SPDX-License-Identifier: Apache-2.0
# Copyright 2021 EPAM Systems
"""
Yocto builder module
"""

import os.path
import shlex
from typing import List, Tuple, cast
from moulin.utils import create_stamp_name
from yaml.nodes import MappingNode, SequenceNode
from moulin import yaml_helpers as yh
from moulin import ninja_syntax


def get_builder(conf: MappingNode, name: str, build_dir: str, src_stamps: List[str],
                generator: ninja_syntax.Writer):
    """
    Return configured YoctoBuilder class
    """
    return YoctoBuilder(conf, name, build_dir, src_stamps, generator)


def gen_build_rules(generator: ninja_syntax.Writer):
    """
    Generate yocto build rules for ninja
    """
    # Create build dir by calling poky/oe-init-build-env script
    cmd = " && ".join([
        "cd $yocto_dir",
        "source poky/oe-init-build-env $work_dir",
    ])
    generator.rule("yocto_init_env",
                   command=f'bash -c "{cmd}"',
                   description="Initialize Yocto build environment")
    generator.newline()

    # Add bitbake layers by calling bitbake-layers script
    cmd = " && ".join([
        "cd $yocto_dir",
        "source poky/oe-init-build-env $work_dir",
        "bitbake-layers add-layer $layers",
        "touch $out",
    ])
    generator.rule("yocto_add_layers",
                   command=f'bash -c "{cmd}"',
                   description="Add yocto layers",
                   pool="console")
    generator.newline()

    # Write own configuration to moulin.conf. Include it in local.conf
    cmd = " && ".join([
        "cd $yocto_dir",
        "echo '# Code generated by moulin. All manual changes will be lost' > $work_dir/conf/moulin.conf",
        "for x in $conf; do echo $$x >> $work_dir/conf/moulin.conf; done",
        "sed \"/require moulin\\.conf/d\" -i $work_dir/conf/local.conf",
        "echo 'require moulin.conf' >> $work_dir/conf/local.conf",
    ])
    generator.rule(
        "yocto_update_conf",
        command=cmd,
        description="Update local.conf",
    )
    generator.newline()

    # Invoke bitbake. This rule uses "console" pool so we can see the bitbake output.
    cmd = " && ".join([
        "cd $yocto_dir",
        "source poky/oe-init-build-env $work_dir",
        "bitbake $target",
    ])
    generator.rule("yocto_build",
                   command=f'bash -c "{cmd}"',
                   description="Yocto Build: $name",
                   pool="console",
                   restat=True)


def _flatten_yocto_conf(conf: SequenceNode) -> List[Tuple[str, str]]:
    """
    Flatten conf entries. While using YAML *entries syntax, we will get list of conf
    entries inside of other list. To overcome this, we need to move inner list 'up'
    """

    # Problem is conf entries that it is list itself
    result: List[Tuple[str, str]] = []
    for entry in conf.value:
        if not isinstance(entry, SequenceNode):
            raise yh.YAMLProcessingError("Exptected array on 'conf' node", entry.start_mark)
        if isinstance(entry.value[0], SequenceNode):
            result.extend([(x.value[0].value, x.value[1].value) for x in entry.value])
        else:
            result.append((entry.value[0].value, entry.value[1].value))
    return result


class YoctoBuilder:
    """
    YoctoBuilder class generates Ninja rules for given build configuration
    """
    def __init__(self, conf: MappingNode, name: str, build_dir: str, src_stamps: List[str],
                 generator: ninja_syntax.Writer):
        self.conf = conf
        self.name = name
        self.generator = generator
        self.src_stamps = src_stamps
        # With yocto builder it is possible to have multiple builds with the same set of
        # layers. Thus, we have two variables - build_dir and work_dir
        # - yocto_dir is the upper directory where layers are stored. Basically, we should
        #   have "poky" in our yocto_dir
        # - work_dir is the build directory where we can find conf/local.conf, tmp and other
        #   directories. It is called "build" by default
        self.yocto_dir = build_dir
        self.work_dir = cast(str, yh.get_str_value(conf, "work_dir", default="build")[0])

    def _get_external_src(self) -> List[Tuple[str, str]]:
        external_src_node = yh.get_mapping_node(self.conf, "external_src")
        if not external_src_node:
            return []

        ret: List[Tuple[str, str]] = []
        for key_node, val_node in external_src_node.value:
            if isinstance(val_node, SequenceNode):
                path = os.path.join(*[cast(str, x.value) for x in val_node.value])
            else:
                path = val_node.value
            path = os.path.abspath(path)
            ret.append((f"EXTERNALSRC_pn-{key_node.value}", path))

        return ret

    def gen_build(self):
        """Generate ninja rules to build yocto/poky"""
        common_variables = {
            "yocto_dir": self.yocto_dir,
            "work_dir": self.work_dir,
        }

        # First we need to ensure that "conf" dir exists
        env_target = os.path.join(self.yocto_dir, self.work_dir, "conf", "local.conf")
        self.generator.build(env_target,
                             "yocto_init_env",
                             self.src_stamps,
                             variables=common_variables)

        # Then we need to add layers
        layers_node = yh.get_sequence_node(self.conf, "layers")
        if layers_node:
            layers = " ".join([x.value for x in layers_node.value])
        else:
            layers = ""
        layers_stamp = create_stamp_name(self.yocto_dir, self.work_dir, "yocto", "layers")
        self.generator.build(layers_stamp,
                             "yocto_add_layers",
                             env_target,
                             variables=dict(common_variables, layers=layers))

        # Next - update local.conf
        local_conf_target = os.path.join(self.yocto_dir, self.work_dir, "conf", "moulin.conf")
        local_conf_node = yh.get_sequence_node(self.conf, "conf")
        if local_conf_node:
            local_conf = _flatten_yocto_conf(local_conf_node)
        else:
            local_conf = []

        # Handle external sources (like build artifacts from some other build)
        local_conf.extend(self._get_external_src())

        # '$' is a ninja escape character so we need to quote it
        local_conf_lines = [
            shlex.quote(f'{k.replace("$", "$$")} = "{v.replace("$", "$$")}"') for k, v in local_conf
        ]

        self.generator.build(local_conf_target,
                             "yocto_update_conf",
                             layers_stamp,
                             variables=dict(common_variables, conf=" ".join(local_conf_lines)))
        self.generator.newline()

        self.generator.build(f"conf-{self.name}", "phony", local_conf_target)
        self.generator.newline()

        # Next step - invoke bitbake. At last :)
        targets = [
            os.path.join(self.yocto_dir, self.work_dir, t.value)
            for t in yh.get_mandatory_sequence(self.conf, "target_images")
        ]
        additional_deps_node = yh.get_sequence_node(self.conf, "additional_deps")
        if additional_deps_node:
            deps = [os.path.join(self.yocto_dir, d.value) for d in additional_deps_node.value]
        else:
            deps = []
        deps.append(local_conf_target)
        self.generator.build(targets,
                             "yocto_build",
                             deps,
                             variables=dict(common_variables,
                                            target=yh.get_mandatory_str_value(
                                                self.conf, "build_target")[0],
                                            name=self.name))

        return targets

    def capture_state(self):
        """
        Update stored local conf with actual SRCREVs for VCS-based recipes.
        This should ensure that we can reproduce this exact build later
        """
