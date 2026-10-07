"""
Public VIF Manager for Graphiant Playbooks.

This module provides functionality for managing gateway Public VIF services — Graphiant-
managed gateway appliances that expose an enterprise LAN segment (VRF) as a "local data
exchange service" to consumer LAN segments, in a chosen region/storage provider.

Unlike Local Extranet and Data Exchange, a Public VIF service is a flat, single-resource
CRUD object (an ``id``) with no separate apply/rollout step of its own — the gateway
appliances host the service directly once created.

Deconfigure workflow consistency (with data_exchange_manager, local_extranet_manager):
- Idempotency: delete_services skips when the service is not found; create_services skips
  when a service with the same name already exists.
- Result shape: delete_services returns changed, deleted, skipped (no 'failed'); create_/
  update_services return changed, created/updated, skipped, diff_plan.
- Logging: "Attempting to delete ..." with target names, then "Deconfigure completed: ..."
  with explicit lists (aligned with data_exchange_manager and local_extranet_manager).
- update_services does not compare against live state before pushing (the nested BGP-
  neighbor/NAT-strategy shape returned by GET is not guaranteed to round-trip byte-for-byte
  with the write shape) — every run pushes the desired payload and reports changed=True,
  the same documented behavior as graphiant_bgp/graphiant_device_config configure operations.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Dict, List, Optional, Tuple

try:
    from tabulate import tabulate

    HAS_TABULATE = True
except ImportError:
    HAS_TABULATE = False

from .base_manager import BaseManager
from .device_config_common import redact_sensitive_for_log
from .logger import setup_logger
from .exceptions import ConfigurationError, DeviceNotFoundError

LOG = setup_logger()


class PublicVifManager(BaseManager):
    """
    Manager for gateway Public VIF service CRUD.
    """

    def configure(
        self,
        config_yaml_file: str,
        vault_public_vif_bgp_md5_passwords: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """
        Configure Public VIF resources based on the provided YAML file.
        This is the main entry point for Public VIF configuration.

        Args:
            config_yaml_file: Path to the YAML configuration file
            vault_public_vif_bgp_md5_passwords: Dict of service name -> device name -> BGP
                MD5 password (pass from Ansible Vault; never written to disk).

        Returns:
            dict: Result with 'changed' status and details of operations performed
        """
        return self.create_services(
            config_yaml_file, vault_public_vif_bgp_md5_passwords=vault_public_vif_bgp_md5_passwords
        )

    def deconfigure(self, config_yaml_file: str) -> dict:
        """
        Deconfigure Public VIF resources based on the provided YAML file.
        This is the main entry point for Public VIF deconfiguration.

        Args:
            config_yaml_file: Path to the YAML configuration file

        Returns:
            dict: Result with 'changed' status and details of operations performed
        """
        return self.delete_services(config_yaml_file)

    def create_services(
        self,
        config_yaml_file: str,
        diff_mode: bool = False,
        vault_public_vif_bgp_md5_passwords: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """
        Create new gateway Public VIF services from YAML configuration.

        Args:
            config_yaml_file (str): Path to the YAML configuration file
            diff_mode (bool): Unused for creation (kept for parity with other managers'
                create_* signature); no drift detection is performed here since a newly
                created service has nothing to drift against.
            vault_public_vif_bgp_md5_passwords: Dict of service name -> device name -> BGP
                MD5 password (pass from Ansible Vault; never written to disk). Used to fill
                in gatewayBgpNeighbors[].md5Password when left null/absent in the YAML (see
                _inject_bgp_md5_vault).

        Returns:
            dict: Result with 'changed' status and lists of created/skipped items
        """
        result: Dict[str, Any] = {"changed": False, "created": [], "skipped": [], "diff_plan": []}
        vault_md5 = vault_public_vif_bgp_md5_passwords if isinstance(vault_public_vif_bgp_md5_passwords, dict) else {}

        try:
            LOG.info("Creating Public VIF services from %s", config_yaml_file)
            config_data = self.render_config_file(config_yaml_file)

            if not config_data or "public_vif_services" not in config_data:
                LOG.info("No public_vif_services configuration found in YAML file")
                return result

            services = config_data["public_vif_services"]
            if not isinstance(services, list):
                raise ConfigurationError("Configuration error: 'public_vif_services' must be a list.")

            for service_config in services:
                service_name = self._service_name(service_config)
                LOG.info("--------------------------------")
                LOG.info("create_services: Creating service '%s'", service_name)

                existing = self.gsdk.get_public_vif_service_by_name(service_name)
                if existing:
                    LOG.info("Service '%s' already exists (ID: %s), skipping creation", service_name, existing.id)
                    result["skipped"].append(service_name)
                    continue

                api_payload = self._build_service_payload(service_config, service_name, vault_md5)

                LOG.info("Service configuration: %s", redact_sensitive_for_log(api_payload))
                result["diff_plan"].append(
                    {
                        "device": service_name,
                        "branch": "create",
                        "before": {},
                        "after": redact_sensitive_for_log(api_payload),
                    }
                )
                created = self.gsdk.create_public_vif_service(api_payload)
                service_id = created.get("id") if isinstance(created, dict) else getattr(created, "id", None)
                LOG.info("Successfully created service '%s' (ID: %s)", service_name, service_id)

                result["created"].append(service_name)
                result["changed"] = True

            LOG.info(
                "Public VIF service creation completed: %s created, %s skipped (changed: %s)",
                len(result["created"]),
                len(result["skipped"]),
                result["changed"],
            )
            return result

        except ConfigurationError:
            raise
        except Exception as e:
            LOG.error("Failed to create Public VIF service: %s", e)
            raise ConfigurationError(f"Public VIF service creation failed: {e}")

    def update_services(
        self,
        config_yaml_file: str,
        vault_public_vif_bgp_md5_passwords: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """
        Update existing gateway Public VIF services from YAML configuration.

        The service must already exist. See the module docstring for why no live-state
        comparison is performed before pushing.

        Args:
            config_yaml_file (str): Path to the YAML configuration file. Each service entry
                requires 'name' plus the full desired configuration.
            vault_public_vif_bgp_md5_passwords: Dict of service name -> device name -> BGP
                MD5 password (pass from Ansible Vault; never written to disk). Used to fill
                in gatewayBgpNeighbors[].md5Password when left null/absent in the YAML (see
                _inject_bgp_md5_vault).

        Returns:
            dict: Result with 'changed' status and lists of updated/skipped items
        """
        result: Dict[str, Any] = {"changed": False, "updated": [], "skipped": [], "diff_plan": []}
        vault_md5 = vault_public_vif_bgp_md5_passwords if isinstance(vault_public_vif_bgp_md5_passwords, dict) else {}

        try:
            LOG.info("Updating Public VIF services from %s", config_yaml_file)
            config_data = self.render_config_file(config_yaml_file)

            if not config_data or "public_vif_services" not in config_data:
                LOG.info("No public_vif_services configuration found in YAML file")
                return result

            services = config_data["public_vif_services"]
            if not isinstance(services, list):
                raise ConfigurationError("Configuration error: 'public_vif_services' must be a list.")

            for service_config in services:
                service_name = self._service_name(service_config)
                LOG.info("--------------------------------")
                LOG.info("update_services: Updating service '%s'", service_name)

                existing = self.gsdk.get_public_vif_service_by_name(service_name)
                if not existing:
                    raise ConfigurationError(
                        f"Service '{service_name}' not found. Use create_services to create new services."
                    )
                service_id = existing.id

                api_payload = self._build_service_payload(service_config, service_name, vault_md5)

                result["diff_plan"].append(
                    {
                        "device": service_name,
                        "branch": "service",
                        "before": {},
                        "after": redact_sensitive_for_log(api_payload),
                    }
                )

                LOG.info(
                    "update_services: Update payload for '%s': %s",
                    service_name,
                    redact_sensitive_for_log(api_payload),
                )
                self.gsdk.edit_public_vif_service(service_id, api_payload)
                LOG.info("Successfully updated service '%s' (ID: %s)", service_name, service_id)

                result["updated"].append(service_name)
                result["changed"] = True

            LOG.info(
                "Public VIF service update completed: %s updated, %s skipped (changed: %s)",
                len(result["updated"]),
                len(result["skipped"]),
                result["changed"],
            )
            return result

        except ConfigurationError:
            raise
        except Exception as e:
            LOG.error("Failed to update Public VIF service: %s", e)
            raise ConfigurationError(f"Public VIF service update failed: {e}")

    def delete_services(self, config_yaml_file: str) -> dict:
        """
        Delete gateway Public VIF services from YAML configuration.

        Args:
            config_yaml_file (str): Path to the YAML configuration file

        Returns:
            dict: Result with 'changed' status and lists of deleted/skipped items
        """
        result: Dict[str, Any] = {"changed": False, "deleted": [], "skipped": []}

        try:
            LOG.info("Deleting Public VIF services from %s", config_yaml_file)
            config_data = self.render_config_file(config_yaml_file)

            if not config_data or "public_vif_services" not in config_data:
                LOG.info("No public_vif_services configuration found in YAML file")
                return result

            services = config_data["public_vif_services"]
            if not isinstance(services, list):
                raise ConfigurationError("Configuration error: 'public_vif_services' must be a list.")

            service_names = [self._service_name(s) for s in services]
            LOG.info("Attempting to delete Public VIF services: %s", service_names)

            for service_config in services:
                service_name = self._service_name(service_config)
                LOG.info("--------------------------------")
                LOG.info("delete_services: Deleting service '%s'", service_name)

                service = self.gsdk.get_public_vif_service_by_name(service_name)
                if not service:
                    LOG.info("Service '%s' not found, skipping deletion", service_name)
                    result["skipped"].append(service_name)
                    continue

                self.gsdk.delete_public_vif_service(service.id)
                LOG.info("Successfully deleted service '%s' (ID: %s)", service_name, service.id)
                result["deleted"].append(service_name)
                result["changed"] = True

            LOG.info(
                "Public VIF service deletion completed: %s deleted, %s skipped (changed: %s)",
                len(result["deleted"]),
                len(result["skipped"]),
                result["changed"],
            )
            LOG.info("Deconfigure completed: deleted=%s, skipped=%s", result["deleted"], result["skipped"])
            return result

        except ConfigurationError:
            raise
        except Exception as e:
            LOG.error("Failed to delete Public VIF service: %s", e)
            raise ConfigurationError(f"Public VIF service deletion failed: {e}")

    def get_services_summary(self) -> Dict[str, Any]:
        """
        Get summary of all gateway Public VIF services.

        Returns:
            dict: {"services": [...]} summary
        """
        try:
            LOG.info("Retrieving Public VIF services summary")
            services = self.gsdk.get_public_vif_services_summary()

            summary = []
            for service in services:
                updated_at = getattr(service, "updated_at", None)
                summary.append(
                    {
                        "id": service.id,
                        "serviceName": service.service_name,
                        "userName": getattr(service, "user_name", None),
                        "updatedAt": str(updated_at) if updated_at is not None else None,
                    }
                )

            if summary and HAS_TABULATE:
                LOG.info(
                    "Public VIF Services Summary:\n%s",
                    tabulate(
                        [[s["id"], s["serviceName"], s["userName"], s["updatedAt"]] for s in summary],
                        headers=["ID", "Service Name", "Created By", "Updated At"],
                        tablefmt="grid",
                    ),
                )

            return {"services": summary}
        except Exception as e:
            LOG.error("Failed to retrieve Public VIF services summary: %s", e)
            raise ConfigurationError(f"Failed to retrieve Public VIF services summary: {e}")

    def get_service_details(self, service_name: str) -> Dict[str, Any]:
        """
        Get detailed configuration for a specific gateway Public VIF service.

        Args:
            service_name (str): Name of the service to retrieve

        Returns:
            dict: Service details

        Raises:
            ConfigurationError: If the service cannot be found.
        """
        try:
            LOG.info("Retrieving Public VIF service '%s'", service_name)
            service = self.gsdk.get_public_vif_service_by_name(service_name)
            if not service:
                raise ConfigurationError(f"Service '{service_name}' not found.")
            return self.gsdk.get_public_vif_service_details(service.id)
        except ConfigurationError:
            raise
        except Exception as e:
            LOG.error("Failed to retrieve service '%s': %s", service_name, e)
            raise ConfigurationError(f"Failed to retrieve service '{service_name}': {e}")

    # --- Internal helpers ---

    @staticmethod
    def _service_name(service_config: dict) -> str:
        """Read 'serviceName' (primary, matches the API field) or the legacy 'name' key."""
        service_name = service_config.get("serviceName") or service_config.get("name")
        if not service_name:
            raise ConfigurationError("Configuration error: Each service must have a 'serviceName' field.")
        return service_name

    def _build_service_payload(
        self,
        service_config: dict,
        service_name: str,
        vault_public_vif_bgp_md5_passwords: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """
        Build the API-shaped Public VIF write payload (``ManaV2PublicVifGatewayWriteRequest``)
        from user-friendly YAML, resolving names to IDs where the collection already has a
        lookup (LAN segment, region, site).

        ``gatewayBgpNeighbors`` (dict keys) and ``natPrefixStrategy`` (a flat ``{device:
        prefix}`` dict, always wrapped as ``centralized`` — see _build_nat_prefix_strategy)
        share the same device-name key space, resolved to device IDs (see
        _resolve_device_key). ``consumerLanSegments`` dict keys are LAN segment names,
        resolved to LAN segment IDs the same way as the top-level ``lanSegment`` field (see
        _resolve_lan_segment_key) — a SEPARATE key space, not name-resolved against devices.
        Each ``gatewayBgpNeighbors`` entry's flat/shorthand fields (``localInterface``,
        ``holdTimerValue``, ``keepaliveTimerValue``, ``multiHop``, ``maxPrefixValue``,
        ``allowAsIn``, ``addressFamilies.<af>.inboundPolicy``/``outboundPolicy``) are expanded
        into the API's nested shape (see _expand_bgp_neighbor); everything else is passed
        through as given — EXCEPT ``enabled``/``bfd``, which are always forced to fixed values
        (see _apply_static_bgp_neighbor_fields), and ``md5Password``, which is vault-filled
        when null/absent (see _inject_bgp_md5_vault). See the module's DOCUMENTATION notes.

        After ``gatewayBgpNeighbors`` is resolved, every device ID is checked against the
        gateway appliances actually provisioned for the resolved ``regionId``/
        ``storageProvider`` (see _validate_gateway_devices), and the resolved producer
        ``lanSegment`` is checked against the LAN segments actually configured on those same
        gateway devices (see _validate_lan_segment_for_gateways) — a LAN segment name that
        resolves fine elsewhere in the tenant but isn't provisioned on these gateways fails
        fast with a clear error instead of silently targeting the wrong VRF.

        Args:
            service_config (dict): Raw YAML entry for one service.
            service_name (str): Resolved service name, for error reporting.
            vault_public_vif_bgp_md5_passwords: Dict of service name -> device name -> BGP
                MD5 password, used to fill ``gatewayBgpNeighbors[].md5Password`` when the
                YAML value is null/absent.

        Returns:
            dict: API-shaped payload (``serviceName``, ``lanSegmentId``, ``regionId``,
                ``storageProvider``, ``consumerLanSegments``, ``gatewayBgpNeighbors``,
                ``natPrefixStrategy``, ``advertisement`` (always included; empty
                ``sites``/``siteLists`` when not given in config), optional
                ``coveringPrefixes``).

        Raises:
            ConfigurationError: If a required field is missing, a referenced name (LAN
                segment, region, site) cannot be resolved, a ``gatewayBgpNeighbors`` device
                isn't a gateway appliance provisioned for the resolved region/storage
                provider, or the resolved ``lanSegment`` isn't configured on those gateway
                devices.
        """
        api_payload: Dict[str, Any] = {"serviceName": service_name}

        lan_segment = service_config.get("lanSegment", service_config.get("lanSegmentId"))
        if lan_segment is None:
            raise ConfigurationError(f"Service '{service_name}': 'lanSegment' is required.")
        api_payload["lanSegmentId"] = self._resolve_lan_segment(lan_segment, service_name)

        region = service_config.get("region", service_config.get("regionId"))
        if region is None:
            raise ConfigurationError(f"Service '{service_name}': 'region' is required.")
        api_payload["regionId"] = self._resolve_region(region, service_name)

        storage_provider = service_config.get("storageProvider")
        if not storage_provider:
            raise ConfigurationError(f"Service '{service_name}': 'storageProvider' is required.")
        api_payload["storageProvider"] = storage_provider

        consumer_lan_segments = service_config.get("consumerLanSegments")
        if not isinstance(consumer_lan_segments, dict) or not consumer_lan_segments:
            raise ConfigurationError(f"Service '{service_name}': 'consumerLanSegments' (dict) is required.")
        api_payload["consumerLanSegments"] = self._resolve_lan_segment_keyed_dict(consumer_lan_segments, service_name)

        gateway_bgp_neighbors = service_config.get("gatewayBgpNeighbors")
        if not isinstance(gateway_bgp_neighbors, dict) or not gateway_bgp_neighbors:
            raise ConfigurationError(f"Service '{service_name}': 'gatewayBgpNeighbors' (dict) is required.")
        api_payload["gatewayBgpNeighbors"] = self._build_gateway_bgp_neighbors(
            gateway_bgp_neighbors, service_name, vault_public_vif_bgp_md5_passwords
        )
        self._validate_gateway_devices(
            api_payload["regionId"], storage_provider, api_payload["gatewayBgpNeighbors"], service_name
        )
        self._validate_lan_segment_for_gateways(
            api_payload["lanSegmentId"],
            [int(device_id) for device_id in api_payload["gatewayBgpNeighbors"]],
            storage_provider,
            service_name,
        )

        nat_prefix_strategy = service_config.get("natPrefixStrategy")
        if not isinstance(nat_prefix_strategy, dict) or not nat_prefix_strategy:
            raise ConfigurationError(f"Service '{service_name}': 'natPrefixStrategy' (dict) is required.")
        api_payload["natPrefixStrategy"] = self._build_nat_prefix_strategy(nat_prefix_strategy, service_name)

        covering_prefixes = service_config.get("coveringPrefixes")
        if covering_prefixes is not None:
            self._validate_cidr_prefixes(covering_prefixes, service_name, "coveringPrefixes")
            api_payload["coveringPrefixes"] = covering_prefixes

        advertisement = service_config.get("advertisement")
        api_payload["advertisement"] = self._resolve_advertisement(
            advertisement if isinstance(advertisement, dict) else {}, service_name
        )

        return api_payload

    def _resolve_lan_segment(self, lan_segment: Any, service_name: str) -> int:
        """Resolve a LAN segment name to its ID; pass through if already an ID."""
        if isinstance(lan_segment, str):
            lan_segment_id = self.gsdk.get_lan_segment_id(lan_segment)
            if not lan_segment_id:
                raise ConfigurationError(f"LAN segment '{lan_segment}' not found for service '{service_name}'.")
            return lan_segment_id
        return lan_segment

    def _resolve_region(self, region: Any, service_name: str) -> int:
        """Resolve a Graphiant region name to its ID; pass through if already an ID."""
        if isinstance(region, str):
            region_id = self.gsdk.get_region_id_by_name(region)
            if not region_id:
                raise ConfigurationError(f"Region '{region}' not found for service '{service_name}'.")
            return region_id
        return region

    def _resolve_lan_segment_key(self, key: Any, service_name: str) -> str:
        """
        Resolve a single ``consumerLanSegments`` dict key to a LAN segment ID string. A
        purely numeric key (e.g. ``"547944"`` or the int ``547944``) is assumed to already
        be a resolved LAN segment ID and is passed through unchanged (as a string, matching
        the API's ``Dict[str, ...]`` key shape); any other string is treated as a LAN
        segment name and resolved via ``get_lan_segment_id`` (the same lookup used for the
        top-level ``lanSegment`` field — see _resolve_lan_segment, and
        ``data_exchange_manager``'s ``serviceLanSegment`` resolution for the same pattern).

        Args:
            key (Any): Raw dict key from the YAML (LAN segment name, numeric string, or int).
            service_name (str): Service name, for error reporting.

        Returns:
            str: The resolved LAN segment ID, as a string.

        Raises:
            ConfigurationError: If a LAN segment name cannot be found.
        """
        key_str = str(key)
        if key_str.isdigit():
            return key_str
        lan_segment_id = self.gsdk.get_lan_segment_id(key_str)
        if not lan_segment_id:
            raise ConfigurationError(f"LAN segment '{key_str}' not found for service '{service_name}'.")
        return str(lan_segment_id)

    def _resolve_lan_segment_keyed_dict(self, mapping: dict, service_name: str) -> dict:
        """Resolve every LAN-segment-name key in *mapping* to a LAN segment ID string (see _resolve_lan_segment_key)."""
        return {self._resolve_lan_segment_key(key, service_name): value for key, value in mapping.items()}

    def _resolve_device_key(self, key: Any, service_name: str, context: str) -> str:
        """
        Resolve a single ``gatewayBgpNeighbors``/``natPrefixStrategy`` dict key to a device ID
        string. A purely numeric key (e.g. ``"30000057493"`` or the int ``30000057493``) is
        assumed to already be a resolved device ID and is passed through unchanged (as a
        string, matching the API's ``Dict[str, ...]`` key shape); any other string is treated
        as a device name and resolved via ``get_device_id`` (raises ``DeviceNotFoundError`` if
        not found), the same convention used for device names elsewhere in this collection
        (e.g. ``local_extranet_manager._resolve_device_names``).

        Args:
            key (Any): Raw dict key from the YAML (device name, numeric string, or int).
            service_name (str): Service name, for error reporting.
            context (str): Where this key came from (e.g. "gatewayBgpNeighbors"), for error
                reporting.

        Returns:
            str: The resolved device ID, as a string.
        """
        key_str = str(key)
        if key_str.isdigit():
            return key_str
        try:
            device_id = self.get_device_id(key_str)
        except DeviceNotFoundError as e:
            raise DeviceNotFoundError(f"Service '{service_name}': {context}: {e}") from e
        return str(device_id)

    def _resolve_device_keyed_dict(self, mapping: dict, service_name: str, context: str) -> dict:
        """Resolve every device-name key in *mapping* to a device ID string (see _resolve_device_key)."""
        return {self._resolve_device_key(key, service_name, context): value for key, value in mapping.items()}

    def _build_gateway_bgp_neighbors(
        self,
        gateway_bgp_neighbors: dict,
        service_name: str,
        vault_public_vif_bgp_md5_passwords: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """
        Expand each neighbor's flat/shorthand fields into the API's nested shape (see
        _expand_bgp_neighbor), fill vault-backed MD5 passwords (see _inject_bgp_md5_vault),
        resolve ``gatewayBgpNeighbors`` device-name keys to IDs (see
        _resolve_device_keyed_dict), and force ``enabled``/``bfd`` to fixed values on every
        neighbor entry (see _apply_static_bgp_neighbor_fields) — these two fields are not
        user-configurable.

        MD5 vault injection runs BEFORE device-name-to-ID resolution, since the vault dict is
        keyed by the raw device name (matching the YAML key space), not the resolved ID.

        Args:
            gateway_bgp_neighbors (dict): Raw ``gatewayBgpNeighbors`` block, keyed by device
                name or ID.
            service_name (str): Service name, for error reporting and as the vault's outer key.
            vault_public_vif_bgp_md5_passwords: Dict of service name -> device name -> BGP
                MD5 password.

        Returns:
            dict: Resolved ``gatewayBgpNeighbors`` with every entry's ``enabled``/``bfd``
                forced to their fixed values and ``md5Password`` vault-filled where applicable.

        Raises:
            ConfigurationError: If a neighbor's ``localInterface`` doesn't exist on its
                resolved gateway device (see _validate_gateway_local_interface).
        """
        expanded = {key: self._expand_bgp_neighbor(value) for key, value in gateway_bgp_neighbors.items()}
        with_vault_md5 = self._inject_bgp_md5_vault(expanded, service_name, vault_public_vif_bgp_md5_passwords or {})
        resolved = self._resolve_device_keyed_dict(with_vault_md5, service_name, "gatewayBgpNeighbors")
        for device_id, neighbor_config in resolved.items():
            self._validate_gateway_local_interface(device_id, neighbor_config, service_name)
        return {key: self._apply_static_bgp_neighbor_fields(value) for key, value in resolved.items()}

    def _validate_gateway_local_interface(self, device_id: str, neighbor_config: dict, service_name: str) -> None:
        """
        Verify a single ``gatewayBgpNeighbors`` entry's ``localInterface`` exists on its
        resolved gateway device, raising a clear error otherwise — the same convention as
        ``DhcpRelayInterfaceManager._validate_interface_entry``.

        Args:
            device_id (str): Resolved gateway device ID (see _resolve_device_key).
            neighbor_config (dict): A single expanded ``gatewayBgpNeighbors`` entry — expects
                ``localInterface`` (if given) already expanded to ``{"interface": "<name>"}``
                (see _expand_bgp_neighbor).
            service_name (str): Service name, for error reporting.

        Raises:
            ConfigurationError: If ``localInterface`` is given but not found among the
                device's interfaces/subinterfaces.
        """
        local_interface = neighbor_config.get("localInterface")
        interface_name = local_interface.get("interface") if isinstance(local_interface, dict) else None
        if not interface_name:
            return

        gcs_device_info = self.gsdk.get_device_info(int(device_id))
        known = self._known_device_interface_names(gcs_device_info)
        if interface_name not in known:
            known_msg = ", ".join(known) if known else "(none found on this device)"
            raise ConfigurationError(
                f"Service '{service_name}': gatewayBgpNeighbors device '{device_id}' references "
                f"localInterface {interface_name!r} which does not exist on this device. "
                f"Known interfaces: {known_msg}."
            )

    @staticmethod
    def _known_device_interface_names(gcs_device_info: Any) -> List[str]:
        """
        Build the sorted list of known interface/subinterface names for a device (e.g.
        ``"GigabitEthernet6/0/0"``, ``"GigabitEthernet6/0/0.100"``), matching the convention
        used by ``DhcpRelayInterfaceManager._list_known_interfaces``.

        Args:
            gcs_device_info: Raw ``get_device_info`` response object (or ``None``).

        Returns:
            list: Sorted interface/subinterface names; empty if the device has none or
                *gcs_device_info* is ``None``/malformed.
        """
        names: set = set()
        device = getattr(gcs_device_info, "device", None)
        if not device:
            return []
        for interface in getattr(device, "interfaces", None) or []:
            parent = getattr(interface, "name", None)
            if parent:
                names.add(str(parent))
            for subintf in getattr(interface, "subinterfaces", None) or []:
                vlan = getattr(subintf, "vlan", None)
                if parent is not None and vlan is not None:
                    names.add(f"{parent}.{vlan}")
        return sorted(names)

    def _validate_gateway_devices(
        self, region_id: int, storage_provider: str, gateway_bgp_neighbors: dict, service_name: str
    ) -> None:
        """
        Verify every ``gatewayBgpNeighbors`` device ID is an actual gateway appliance
        provisioned for this service's region/storage provider (GET
        ``/v1/regions/{region_id}/gateways``), raising a clear error naming the offending
        device(s) instead of letting a bad ``lanSegment``/device combination fail
        confusingly at the API.

        Args:
            region_id (int): Resolved Graphiant region ID.
            storage_provider (str): Storage provider (e.g. "AWS").
            gateway_bgp_neighbors (dict): Resolved ``gatewayBgpNeighbors`` (string device ID
                keys — see _resolve_device_key).
            service_name (str): Service name, for error reporting.

        Raises:
            ConfigurationError: If any key is not a valid gateway appliance device ID for
                this region/storage provider.
        """
        available_gateways = self.gsdk.get_public_vif_gateways(region_id, storage_provider)
        available_device_ids = {str(getattr(gateway, "device_id", None)) for gateway in available_gateways}

        invalid_device_ids = [key for key in gateway_bgp_neighbors if key not in available_device_ids]
        if invalid_device_ids:
            available_desc = (
                ", ".join(
                    f"{getattr(gateway, 'hostname', None)} ({getattr(gateway, 'device_id', None)})"
                    for gateway in available_gateways
                )
                or "none"
            )
            raise ConfigurationError(
                f"Service '{service_name}': gatewayBgpNeighbors device ID(s) "
                f"{', '.join(invalid_device_ids)} are not gateway appliances provisioned for "
                f"region ID {region_id} / storage provider '{storage_provider}'. "
                f"Available gateways: {available_desc}."
            )

    def _validate_lan_segment_for_gateways(
        self, lan_segment_id: int, gateway_device_ids: List[int], storage_provider: str, service_name: str
    ) -> None:
        """
        Verify the resolved producer ``lanSegment`` is actually configured on the
        ``gatewayBgpNeighbors`` devices for this storage provider (GET
        ``/v1/lan-segments?deviceIds[]=...&gatewayCloudProvider=...``, see
        _resolve_lan_segment/gcsdk_client.get_lan_segments_for_gateways), raising a clear
        error instead of silently accepting a LAN segment name that resolves fine elsewhere
        in the tenant but isn't actually provisioned on these specific gateway devices.

        Args:
            lan_segment_id (int): Resolved producer LAN segment (VRF) ID.
            gateway_device_ids (list): Resolved ``gatewayBgpNeighbors`` device IDs.
            storage_provider (str): Storage provider (e.g. "AWS").
            service_name (str): Service name, for error reporting.

        Raises:
            ConfigurationError: If ``lan_segment_id`` isn't among the LAN segments available
                for these gateway devices/storage provider.
        """
        available_segments = self.gsdk.get_lan_segments_for_gateways(gateway_device_ids, storage_provider)
        available_ids = {getattr(segment, "id", None) for segment in available_segments}

        if lan_segment_id not in available_ids:
            available_desc = (
                ", ".join(
                    f"{getattr(segment, 'name', None)} ({getattr(segment, 'id', None)})"
                    for segment in available_segments
                )
                or "none"
            )
            raise ConfigurationError(
                f"Service '{service_name}': lanSegment ID {lan_segment_id} is not configured "
                f"on the gatewayBgpNeighbors devices for storage provider '{storage_provider}'. "
                f"Available LAN segments: {available_desc}."
            )

    @staticmethod
    def _set_nested(target: Dict[str, Any], path: Tuple[str, ...], value: Any) -> None:
        """Set target[path[0]][path[1]]...[path[-1]] = value, creating intermediate dicts as needed."""
        cur = target
        for key in path[:-1]:
            nxt = cur.get(key)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[key] = nxt
            cur = nxt
        cur[path[-1]] = value

    @classmethod
    def _expand_bgp_neighbor(cls, neighbor_config: dict) -> dict:
        """
        Expand a single ``gatewayBgpNeighbors`` entry's flat/shorthand fields into the API's
        nested shape, using _set_nested:
          - ``localInterface: "<iface>"`` -> ``{"localInterface": {"interface": "<iface>"}}``
          - ``holdTimerValue: <n>`` -> ``{"holdTimerValue": {"timer": <n>}}``
          - ``keepaliveTimerValue: <n>`` -> ``{"keepaliveTimerValue": {"timer": <n>}}``
          - ``multiHop: <n>`` -> ``{"ebgpMultihopTtl": {"multiHop": <n>}}``
          - ``maxPrefixValue: <n>`` -> ``{"maxPrefixValue": {"maxPrefix": <n>}}``
          - ``allowAsIn: <n>`` -> ``{"allowAsIn": {"count": <n>}}``
          - ``addressFamilies: {ipv4: {inboundPolicy: <name>, outboundPolicy: <name>}, ...}``
            -> each family expands to
            ``{"family": {"addressFamily": "ipv4", "inboundPolicy": {"policy": <name>} or {},
            "outboundPolicy": {"policy": <name>} or {}}}``.

        ``md5Password`` is left untouched here — _inject_bgp_md5_vault already normalizes a
        flat string into the nested ``{"md5Password": ...}`` shape. Everything else (e.g.
        ``peerAsn``, ``remoteAddress``, ``asOverride``) is passed through unchanged.

        Args:
            neighbor_config (dict): Raw per-neighbor BGP config from the YAML (flat/shorthand
                fields, camelCase, matching the API's field names but not its nesting).

        Returns:
            dict: A copy of *neighbor_config* with the shorthand fields expanded.
        """
        expanded: Dict[str, Any] = dict(neighbor_config)

        local_interface = expanded.pop("localInterface", None)
        if local_interface is not None:
            cls._set_nested(expanded, ("localInterface", "interface"), local_interface)

        hold_timer = expanded.pop("holdTimerValue", None)
        if hold_timer is not None:
            cls._set_nested(expanded, ("holdTimerValue", "timer"), hold_timer)

        keepalive_timer = expanded.pop("keepaliveTimerValue", None)
        if keepalive_timer is not None:
            cls._set_nested(expanded, ("keepaliveTimerValue", "timer"), keepalive_timer)

        multi_hop = expanded.pop("multiHop", None)
        if multi_hop is not None:
            cls._set_nested(expanded, ("ebgpMultihopTtl", "multiHop"), multi_hop)

        max_prefix = expanded.pop("maxPrefixValue", None)
        if max_prefix is not None:
            cls._set_nested(expanded, ("maxPrefixValue", "maxPrefix"), max_prefix)

        allow_as_in = expanded.pop("allowAsIn", None)
        if allow_as_in is not None:
            cls._set_nested(expanded, ("allowAsIn", "count"), allow_as_in)

        address_families = expanded.pop("addressFamilies", None)
        if isinstance(address_families, dict):
            for family_name, family_config in address_families.items():
                family_config = family_config or {}
                inbound_policy = family_config.get("inboundPolicy")
                outbound_policy = family_config.get("outboundPolicy")
                cls._set_nested(expanded, ("addressFamilies", family_name, "family", "addressFamily"), family_name)
                cls._set_nested(
                    expanded,
                    ("addressFamilies", family_name, "family", "inboundPolicy"),
                    {"policy": inbound_policy} if inbound_policy else {},
                )
                cls._set_nested(
                    expanded,
                    ("addressFamilies", family_name, "family", "outboundPolicy"),
                    {"policy": outbound_policy} if outbound_policy else {},
                )

        return expanded

    @staticmethod
    def _inject_bgp_md5_vault(
        gateway_bgp_neighbors: Dict[str, Any], service_name: str, vault_md5: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Fill each neighbor's ``md5Password`` from vault when left null/absent in the YAML.

        Precedence (YAML wins if non-null; vault fills null/absent) — the same convention
        used for OSPFv2 interface MD5 keys (see OSPFv2Manager._build_interface) and
        Site-to-Site VPN BGP MD5 passwords (see SiteToSiteVpnManager._inject_vault_secrets):
          1. YAML non-null value -> used as-is (dev/local testing only; do not commit secrets)
          2. YAML null or absent -> looked up from vault, keyed by service name -> device name
             (the raw ``gatewayBgpNeighbors`` key, before device-name-to-ID resolution)
          3. Not in YAML AND not in vault -> ``md5Password`` left unset (no BGP MD5 auth
             configured for that neighbor)

        A plain string ``md5Password`` value is normalized to the API's nested
        ``{"md5Password": <value>}`` shape, same as a vault-filled value.

        Args:
            gateway_bgp_neighbors (dict): Raw ``gatewayBgpNeighbors`` block, keyed by device
                name or ID.
            service_name (str): Service name, used as the vault's outer key.
            vault_md5 (dict): Dict of service name -> device name -> MD5 password string.

        Returns:
            dict: A copy of ``gateway_bgp_neighbors`` (each neighbor entry copied, not
                mutated in place) with ``md5Password`` vault-filled where applicable.
        """
        vault_for_service = vault_md5.get(service_name) if isinstance(vault_md5, dict) else None
        vault_for_service = vault_for_service if isinstance(vault_for_service, dict) else {}

        injected: Dict[str, Any] = {}
        for device_key, neighbor_config in gateway_bgp_neighbors.items():
            neighbor_config = dict(neighbor_config) if isinstance(neighbor_config, dict) else {}
            has_md5_key = "md5Password" in neighbor_config
            md5_val = neighbor_config.get("md5Password")

            if isinstance(md5_val, str):
                md5_val = {"md5Password": md5_val}

            if md5_val is None:
                vault_password = vault_for_service.get(device_key)
                if vault_password:
                    md5_val = {"md5Password": str(vault_password)}
                    has_md5_key = True
                    LOG.debug("Injected md5Password for service '%s' device '%s' from vault", service_name, device_key)

            if has_md5_key:
                neighbor_config["md5Password"] = md5_val
            injected[device_key] = neighbor_config

        return injected

    @staticmethod
    def _apply_static_bgp_neighbor_fields(neighbor_config: dict) -> dict:
        """
        Force ``enabled`` and ``bfd`` to fixed values on a single ``gatewayBgpNeighbors``
        entry, overriding whatever the user supplied (modifies a copy, not in place).

        Neither is user-configurable in practice: BFD is not yet enabled for Public VIF
        gateway neighbors (always sent as ``{"bfd": {"enabled": false}}``), and there is no
        portal UI control to disable a configured neighbor (always sent as ``enabled: true``).

        Args:
            neighbor_config (dict): Raw per-neighbor BGP config from the YAML.

        Returns:
            dict: A copy of *neighbor_config* with ``enabled``/``bfd`` forced.
        """
        resolved = dict(neighbor_config)
        resolved["enabled"] = True
        resolved["bfd"] = {"bfd": {"enabled": False}}
        return resolved

    def _build_nat_prefix_strategy(self, nat_prefix_strategy: dict, service_name: str) -> dict:
        """
        Build the API's ``natPrefixStrategy.centralized.consumerPrefix`` shape from a flat
        ``{device: prefix}`` dict — the only strategy exposed to users (the API's
        ``decentralized`` alternative is not surfaced by this module; always ``centralized``).
        Keys share ``gatewayBgpNeighbors``' key space (device name or ID, confirmed via a live
        POST/GET capture), not ``consumerLanSegments`` — see _resolve_device_keyed_dict.

        Args:
            nat_prefix_strategy (dict): Flat ``{device: prefix}`` dict from the YAML.
            service_name (str): Service name, for error reporting.

        Returns:
            dict: ``{"centralized": {"consumerPrefix": {<resolved device id>: <prefix>, ...}}}``.
        """
        resolved_prefixes = self._resolve_device_keyed_dict(nat_prefix_strategy, service_name, "natPrefixStrategy")
        return {"centralized": {"consumerPrefix": resolved_prefixes}}

    def _resolve_advertisement(self, advertisement: dict, service_name: str) -> dict:
        """
        Resolve 'advertisement.sites'/'siteLists' names to IDs (modifies a copy, not in
        place). Both default to an empty list when missing/not a list — the API expects
        'advertisement' to always carry both keys (an empty pair means "advertise to all
        symmetric sites", confirmed via a live POST/GET capture), so this is sent even when
        the config omits 'advertisement' entirely (see _build_service_payload).
        """
        resolved = dict(advertisement)
        site_names = resolved.get("sites")
        resolved["sites"] = (
            [self.get_site_id(site) if isinstance(site, str) else site for site in site_names]
            if isinstance(site_names, list)
            else []
        )
        site_list_names = resolved.get("siteLists")
        resolved["siteLists"] = (
            [
                self._resolve_site_list(site_list, service_name) if isinstance(site_list, str) else site_list
                for site_list in site_list_names
            ]
            if isinstance(site_list_names, list)
            else []
        )
        return resolved

    def _resolve_site_list(self, site_list_name: str, service_name: str) -> int:
        """Resolve a site list name to its ID; raises if not found."""
        site_list_id = self.gsdk.get_site_list_id(site_list_name)
        if not site_list_id:
            raise ConfigurationError(f"Site list '{site_list_name}' not found for service '{service_name}'.")
        return site_list_id

    @staticmethod
    def _validate_cidr_prefixes(prefixes: list, service_name: str, context: str) -> None:
        """
        Validate that each prefix is a properly-aligned CIDR network address (host bits
        zero), matching the portal UI's own validation (see
        LocalExtranetManager._validate_cidr_prefixes for the equivalent Local Extranet check).

        Args:
            prefixes (list): Prefix strings to validate.
            service_name (str): Service name, for error reporting.
            context (str): Where these prefixes came from (e.g. "coveringPrefixes").
        """
        for prefix in prefixes or []:
            if not isinstance(prefix, str):
                continue
            try:
                ipaddress.ip_network(prefix, strict=True)
            except ValueError:
                try:
                    corrected = str(ipaddress.ip_network(prefix, strict=False))
                    hint = f" (e.g. '{corrected}')"
                except ValueError:
                    hint = ""
                raise ConfigurationError(
                    f"Service '{service_name}': invalid {context} prefix '{prefix}'. Please make sure the "
                    f"network address of the CIDR is provided{hint}."
                )
