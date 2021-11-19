import logging
import os
import re
import requests
import subprocess
import uuid
from pathlib import Path
from typing import List

from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from statemachine import RetryingStateMachine
from swarmexecutor import SwarmExecutor
from swarmkubecache import SwarmKubeCache
from withcontainerconfigs import WithContainerConfigs


SCRIPT_DIR = Path(__file__).parent


@dataclass
class SwarmAgentConfig:
    """
    Configuration which applies to all agents within a swarm.

    This is the configuration which is generated by Swarm and then
    passed on to Cluster so it can later pass it to the Agents.
    """

    agent_binary: Path
    agent_image_path: str
    ca_cert_path: Path
    token: str
    ssh_pub_key: str
    pull_secret: str
    service_url: str
    shared_storage: Path
    executor: SwarmExecutor
    logging: logging.Logger
    shared_graphroot: Path
    k8s_api_server_url: str
    kube_cache: SwarmKubeCache
    num_locks: int


@dataclass
class ClusterAgentConfig:
    """
    Configuration which applies to a particular agent within a cluster.

    This is generated by the Cluster for every one of its agents
    """

    index: int
    mac_address: str
    identifier: str
    machine_hostname: str
    machine_ip: str
    cluster_identifier: str
    cluster_dir: Path
    cluster_hostnames: List[str]
    cluster_ips: List[str]


class Agent(RetryingStateMachine, WithContainerConfigs):
    """
    A state machine to execute the commands for a single swarm agent
    """

    def __init__(
        self,
        swarm_agent_config: SwarmAgentConfig,
        cluster_agent_config: ClusterAgentConfig,
    ):
        super().__init__(
            initial_state="Initializing",
            terminal_state="Done",
            states=OrderedDict(
                {
                    "Initializing": self.initialize,
                    "Waiting for ISO URL on InfraEnv": self.wait_iso_url_infraenv,
                    'Seting BMH provisioning state to "ready"': self.ready_bmh,
                    "Waiting for ISO URL on BMH": self.wait_iso_url_bmh,
                    "Download ISO": self.download_iso,
                    'Seting BMH provisioning state to "provisioned"': self.provisioned_bmh,
                    "Generating container configurations": self.create_container_configs,
                    "Running agent": self.run_agent,
                    "Done": self.done,
                }
            ),
            logging=logging,
            name=f"Agent {cluster_agent_config.index}",
        )

        self.swarm_agent_config = swarm_agent_config
        self.cluster_agent_config = cluster_agent_config

        # Identifiers
        self.host_id = str(uuid.uuid4())
        self.identifier = cluster_agent_config.identifier

        # Utils
        self.logging = logging

        # Directories
        self.agent_dir = self.cluster_agent_config.cluster_dir / f"agent-{cluster_agent_config.index}"

        # General paths
        self.fake_reboot_marker_path = self.agent_dir / "fake_reboot_marker"

        # Container config
        self.personal_graphroot = self.agent_dir / "graphroot"
        WithContainerConfigs.__init__(
            self,
            self.personal_graphroot,
            self.swarm_agent_config.shared_graphroot,
            self.agent_dir,
            self.swarm_agent_config.num_locks,
        )

        # Endpoints
        self.service_url = swarm_agent_config.service_url
        self.k8s_api_server_url = swarm_agent_config.k8s_api_server_url

        # Logging paths
        self.log_dir = self.agent_dir / "logs"
        self.agent_stdout_path = self.agent_dir / "agent.stdout.logs"
        self.agent_stderr_path = self.agent_dir / "agent.stderr.logs"

    def initialize(self, next_state):
        for dir in (self.agent_dir, self.log_dir, self.personal_graphroot):
            dir.mkdir(parents=True, exist_ok=True)

        return next_state

    def download_iso(self, next_state):
        self.swarm_agent_config.executor.check_call(
            ["curl", "-s", "-o", "/dev/null", self.bmh_iso_url],
            check=True,
        )

        return next_state

    @staticmethod
    def get_infraenv_id_from_url(url):
        uuid_regex = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

        search = re.search(uuid_regex, url)
        if search:
            return search.group(0)

        raise RuntimeError("Could not find infraenv ID from url")

    def wait_iso_url_infraenv(self, next_state):
        infraenv = self.swarm_agent_config.kube_cache.get_infraenv(
            namespace=self.cluster_agent_config.cluster_identifier, name=self.cluster_agent_config.cluster_identifier
        )

        if infraenv is not None:
            iso_url = infraenv.get("status", {}).get("isoDownloadURL", "")

            if iso_url == "":
                self.logging.info("Infraenv .status.isoDownloadURL is empty")
                return self.state

            self.logging.info(f"Infraenv .status.isoDownloadURL found {iso_url}")
            self.infraenv_iso_url = iso_url

            self.infraenv_id = self.get_infraenv_id_from_url(self.infraenv_iso_url)

            return next_state

        self.logging.info(
            f"Infraenv {self.cluster_agent_config.cluster_identifier}/{self.cluster_agent_config.cluster_identifier} not found"
        )

        return self.state

    def wait_iso_url_bmh(self, next_state):
        baremetalhost = self.swarm_agent_config.kube_cache.get_baremetalhost(
            namespace=self.cluster_agent_config.cluster_identifier, name=self.identifier
        )

        if baremetalhost is not None:
            iso_url = baremetalhost.get("spec", {}).get("image", {}).get("url", "")

            if iso_url == "":
                self.logging.info("BMH .spec.image.url is empty")
                return self.state

            self.logging.info(f"BMH .spec.image.url found {iso_url}")
            self.bmh_iso_url = iso_url

            self.infraenv_id = self.get_infraenv_id_from_url(self.infraenv_iso_url)

            return next_state

        self.logging.info(f"BMH {self.cluster_agent_config.cluster_identifier}/{self.identifier} not found")

        return self.state

    def set_bmh_provisioning_state(self, provisioning_state):
        baremetalhost = self.swarm_agent_config.kube_cache.get_baremetalhost(
            namespace=self.cluster_agent_config.cluster_identifier, name=self.identifier
        )

        if baremetalhost is not None:
            baremetalhost["status"] = {
                "errorCount": 0,
                "errorMessage": "",
                "goodCredentials": {},
                "hardwareProfile": "",
                "operationalStatus": "discovered",
                "poweredOn": True,
                "provisioning": {"state": provisioning_state, "ID": "", "image": {"url": ""}},
            }

            response = requests.put(
                f"{self.k8s_api_server_url}/apis/metal3.io/v1alpha1/namespaces/{self.cluster_agent_config.cluster_identifier}/baremetalhosts/{self.identifier}/status",
                json=baremetalhost,
                headers={"Authorization": f"Bearer {self.swarm_agent_config.token}"},
                verify=str(self.swarm_agent_config.ca_cert_path),
            )
            response.raise_for_status()

            return True

        self.logging.info(f"BMH {self.cluster_agent_config.cluster_identifier}/{self.identifier} not found")

        return False

    def ready_bmh(self, next_state):
        if self.set_bmh_provisioning_state("ready"):
            return next_state

        return self.state

    def provisioned_bmh(self, next_state):
        if self.set_bmh_provisioning_state("provisioned"):
            return next_state

        return self.state

    def run_agent(self, next_state):
        agent_environment = {
            "CONTAINERS_CONF": str(self.container_config),
            "CONTAINERS_STORAGE_CONF": str(self.container_storage_conf),
            "PULL_SECRET_TOKEN": self.swarm_agent_config.pull_secret,
            "DRY_ENABLE": "true",
            "DRY_HOST_ID": self.host_id,
            "DRY_FORCED_MAC_ADDRESS": self.cluster_agent_config.mac_address,
            "DRY_FAKE_REBOOT_MARKER_PATH": str(self.fake_reboot_marker_path),
            "DRY_FORCED_HOSTNAME": self.cluster_agent_config.machine_hostname,
            # The installer needs to know all the hostnames in the cluster
            "DRY_HOSTNAMES": ",".join(self.cluster_agent_config.cluster_hostnames),
            "DRY_IPS": ",".join(ip.split("/")[0] for ip in self.cluster_agent_config.cluster_ips),
            "DRY_FORCED_HOST_IPV4": self.cluster_agent_config.machine_ip,
        }

        agent_command = [
            str(self.swarm_agent_config.agent_binary),
            "--url",
            self.service_url,
            "--infra-env-id",
            self.infraenv_id,
            "--agent-version",
            self.swarm_agent_config.agent_image_path,
            "--insecure=true",
            "--cacert",
            str(self.swarm_agent_config.ca_cert_path),
        ]

        with self.agent_stdout_path.open("ab") as agent_stdout_file:
            with self.agent_stderr_path.open("ab") as agent_stderr_file:
                agent_stdout_file.write(
                    f"Running agent with command: {agent_command} and env {agent_environment}".encode("utf-8")
                )
                agent_process = self.swarm_agent_config.executor.Popen(
                    self.swarm_agent_config.executor.prepare_sudo_command(agent_command, agent_environment),
                    env={**os.environ, **agent_environment},
                    stdin=subprocess.DEVNULL,
                    stdout=agent_stdout_file,
                    stderr=agent_stderr_file,
                )

        if agent_process.wait() != 0:
            self.logging.error(f"Agent exited with non-zero exit code {agent_process.returncode}")
            return self.state

        return next_state

    def done(self, _):
        return self.state
