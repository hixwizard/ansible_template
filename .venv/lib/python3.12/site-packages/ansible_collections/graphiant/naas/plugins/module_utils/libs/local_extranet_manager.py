"""
Local Extranet Manager for Graphiant Playbooks.

This module provides functionality for managing Local Extranet policies: sharing a LAN
segment (VRF) with other LAN segments across sites/branches within the same enterprise.

Unlike Data Exchange (cross-enterprise B2B peering via producer/customer/match/invitation
entities), Local Extranet is a flat, single-resource CRUD (one ``policy`` object with an
``id``), scoped entirely within the current tenant.

Deconfigure workflow consistency (with data_exchange_manager, global_config_manager):
- Idempotency: delete_policies skips when the policy is not found.
- Result shape: delete_policies returns changed, deleted, skipped (no 'failed'); create_/
  update_policies return changed, created/updated, skipped, diff_plan.
- Logging: "Attempting to delete ..." with target names, then "Deconfigure completed: ..."
  with explicit lists (aligned with data_exchange_manager and site_manager).
- create_policies/update_policies automatically push the policy to devices (POST .../apply)
  after a successful create/update — there is no separate "apply" operation.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Dict, List, Optional, cast

try:
    from tabulate import tabulate

    HAS_TABULATE = True
except ImportError:
    HAS_TABULATE = False

from .base_manager import BaseManager
from .logger import setup_logger
from .exceptions import ConfigurationError

LOG = setup_logger()


class LocalExtranetManager(BaseManager):
    """
    Manager for Local Extranet policy CRUD and device rollout.
    """

    def configure(self, config_yaml_file: str) -> dict:
        """
        Configure Local Extranet resources based on the provided YAML file.
        This is the main entry point for Local Extranet configuration.

        Args:
            config_yaml_file: Path to the YAML configuration file

        Returns:
            dict: Result with 'changed' status and details of operations performed
        """
        return self.create_policies(config_yaml_file)

    def deconfigure(self, config_yaml_file: str) -> dict:
        """
        Deconfigure Local Extranet resources based on the provided YAML file.
        This is the main entry point for Local Extranet deconfiguration.

        Args:
            config_yaml_file: Path to the YAML configuration file

        Returns:
            dict: Result with 'changed' status and details of operations performed
        """
        return self.delete_policies(config_yaml_file)

    def create_policies(self, config_yaml_file: str, diff_mode: bool = False) -> dict:
        """
        Create new Local Extranet policies from YAML configuration.

        Args:
            config_yaml_file (str): Path to the YAML configuration file
            diff_mode (bool): Unused for creation (kept for parity with other managers'
                create_* signature); no drift detection is performed here since a newly
                created policy has nothing to drift against.

        Returns:
            dict: Result with 'changed' status and lists of created/skipped items
        """
        result: Dict[str, Any] = {"changed": False, "created": [], "skipped": [], "diff_plan": []}

        try:
            LOG.info("Creating Local Extranet policies from %s", config_yaml_file)
            config_data = self.render_config_file(config_yaml_file)

            if not config_data or "local_extranet_policies" not in config_data:
                LOG.info("No local_extranet_policies configuration found in YAML file")
                return result

            policies = config_data["local_extranet_policies"]
            if not isinstance(policies, list):
                raise ConfigurationError("Configuration error: 'local_extranet_policies' must be a list.")

            LOG.info("LocalExtranetManager: Current enterprise info: %s", self.gsdk.enterprise_info)

            for policy_config in policies:
                policy_name = policy_config.get("name")
                LOG.info("--------------------------------")
                LOG.info("create_policies: Creating policy '%s'", policy_name)
                if not policy_name:
                    raise ConfigurationError("Configuration error: Each policy must have a 'name' field.")

                existing_policy = self.gsdk.get_local_extranet_policy_by_name(policy_name)
                if existing_policy:
                    LOG.info("Policy '%s' already exists (ID: %s), skipping creation", policy_name, existing_policy.id)
                    result["skipped"].append(policy_name)
                    continue

                target_device_names = policy_config.pop("targetDevices", None)
                api_policy = {k: v for k, v in policy_config.items() if k != "name"}
                api_policy["name"] = policy_name

                self._resolve_policy_ids(api_policy, policy_name)
                self._validate_policy_prefixes(api_policy, policy_name)

                LOG.info("Policy configuration: %s", api_policy)
                result["diff_plan"].append(
                    {
                        "device": policy_name,
                        "branch": "create",
                        "before": {},
                        "after": api_policy,
                    }
                )
                created = self.gsdk.create_local_extranet_policy(api_policy)
                policy_id = created.get("id") if isinstance(created, dict) else getattr(created, "id", None)
                if policy_id is None:
                    raise ConfigurationError(
                        f"Policy '{policy_name}' was created but no ID was returned by the API; "
                        "cannot apply it to devices."
                    )
                LOG.info("Successfully created policy '%s' (ID: %s)", policy_name, policy_id)

                target_device_ids = self._resolve_device_names(target_device_names, policy_name)
                self.gsdk.apply_local_extranet_policy(policy_id, target_device_ids)
                LOG.info("Applied policy '%s' (ID: %s) to devices", policy_name, policy_id)

                result["created"].append(policy_name)
                result["changed"] = True

            LOG.info(
                "Local Extranet policy creation completed: %s created, %s skipped (changed: %s)",
                len(result["created"]),
                len(result["skipped"]),
                result["changed"],
            )
            return result

        except ConfigurationError:
            raise
        except Exception as e:
            LOG.error("Failed to create Local Extranet policy: %s", e)
            raise ConfigurationError(f"Local Extranet policy creation failed: {e}")

    def update_policies(self, config_yaml_file: str) -> dict:
        """
        Update existing Local Extranet policies from YAML configuration.

        Unlike Data Exchange (where only a single field is mutable per service type), the
        full policy is mutable here. The policy must already exist.

        Args:
            config_yaml_file (str): Path to the YAML configuration file.
                Each policy entry requires 'name' plus any fields to change.

        Returns:
            dict: Result with 'changed' status and lists of updated/skipped items
        """
        result: Dict[str, Any] = {"changed": False, "updated": [], "skipped": [], "diff_plan": []}

        try:
            LOG.info("Updating Local Extranet policies from %s", config_yaml_file)
            config_data = self.render_config_file(config_yaml_file)

            if not config_data or "local_extranet_policies" not in config_data:
                LOG.info("No local_extranet_policies configuration found in YAML file")
                return result

            policies = config_data["local_extranet_policies"]
            if not isinstance(policies, list):
                raise ConfigurationError("Configuration error: 'local_extranet_policies' must be a list.")

            LOG.info("LocalExtranetManager: Current enterprise info: %s", self.gsdk.enterprise_info)

            for policy_config in policies:
                policy_name = policy_config.get("name")
                LOG.info("--------------------------------")
                LOG.info("update_policies: Updating policy '%s'", policy_name)
                if not policy_name:
                    raise ConfigurationError("Configuration error: Each policy must have a 'name' field.")

                existing_policy = self.gsdk.get_local_extranet_policy_by_name(policy_name)
                if not existing_policy:
                    raise ConfigurationError(
                        f"Policy '{policy_name}' not found. Use create_policies to create new policies."
                    )
                policy_id = existing_policy.id

                target_device_names = policy_config.pop("targetDevices", None)
                api_policy = {k: v for k, v in policy_config.items() if k != "name"}
                api_policy["name"] = policy_name

                self._resolve_policy_ids(api_policy, policy_name, for_update=True)
                self._validate_policy_prefixes(api_policy, policy_name)

                current_details = self.gsdk.get_local_extranet_policy_details(policy_id)
                self._carry_over_prefix_set_id(api_policy.get("source"), current_details.get("source") or {})
                self._carry_over_prefix_set_id(api_policy.get("branches"), current_details.get("branches") or {})
                current_normalized = self._normalize_policy(current_details)
                desired_normalized = self._normalize_policy(api_policy)

                if current_normalized == desired_normalized:
                    LOG.info("Policy '%s' unchanged, skipping update", policy_name)
                    result["skipped"].append(policy_name)
                    continue

                result["diff_plan"].append(
                    {
                        "device": policy_name,
                        "branch": "policy",
                        "before": current_normalized,
                        "after": desired_normalized,
                    }
                )

                LOG.info("update_policies: Update payload for '%s': %s", policy_name, api_policy)
                self.gsdk.edit_local_extranet_policy(policy_id, api_policy)
                LOG.info("Successfully updated policy '%s' (ID: %s)", policy_name, policy_id)

                target_device_ids = self._resolve_device_names(target_device_names, policy_name)
                self.gsdk.apply_local_extranet_policy(policy_id, target_device_ids)
                LOG.info("Applied policy '%s' (ID: %s) to devices", policy_name, policy_id)

                result["updated"].append(policy_name)
                result["changed"] = True

            LOG.info(
                "Local Extranet policy update completed: %s updated, %s skipped (changed: %s)",
                len(result["updated"]),
                len(result["skipped"]),
                result["changed"],
            )
            return result

        except ConfigurationError:
            raise
        except Exception as e:
            LOG.error("Failed to update Local Extranet policy: %s", e)
            raise ConfigurationError(f"Local Extranet policy update failed: {e}")

    def delete_policies(self, config_yaml_file: str) -> dict:
        """
        Delete Local Extranet policies from YAML configuration.

        Args:
            config_yaml_file (str): Path to the YAML configuration file

        Returns:
            dict: Result with 'changed' status and lists of deleted/skipped items
        """
        result: Dict[str, Any] = {"changed": False, "deleted": [], "skipped": []}

        try:
            LOG.info("Deleting Local Extranet policies from %s", config_yaml_file)
            config_data = self.render_config_file(config_yaml_file)

            if not config_data or "local_extranet_policies" not in config_data:
                LOG.info("No local_extranet_policies configuration found in YAML file")
                return result

            policies = config_data["local_extranet_policies"]
            if not isinstance(policies, list):
                raise ConfigurationError("Configuration error: 'local_extranet_policies' must be a list.")

            policy_names = [p.get("name") for p in policies if p.get("name")]
            LOG.info("Attempting to delete Local Extranet policies: %s", policy_names)
            LOG.info("LocalExtranetManager: Current enterprise info: %s", self.gsdk.enterprise_info)

            for policy_config in policies:
                policy_name = policy_config.get("name")
                LOG.info("--------------------------------")
                LOG.info("delete_policies: Deleting policy '%s'", policy_name)
                if not policy_name:
                    raise ConfigurationError("Configuration error: Each policy must have a 'name' field.")

                policy = self.gsdk.get_local_extranet_policy_by_name(policy_name)
                if not policy:
                    LOG.info("Policy '%s' not found, skipping deletion", policy_name)
                    result["skipped"].append(policy_name)
                    continue

                self.gsdk.delete_local_extranet_policy(policy.id)
                LOG.info("Successfully deleted policy '%s' (ID: %s)", policy_name, policy.id)
                result["deleted"].append(policy_name)
                result["changed"] = True

            LOG.info(
                "Local Extranet policy deletion completed: %s deleted, %s skipped (changed: %s)",
                len(result["deleted"]),
                len(result["skipped"]),
                result["changed"],
            )
            LOG.info("Deconfigure completed: deleted=%s, skipped=%s", result["deleted"], result["skipped"])
            return result

        except ConfigurationError:
            raise
        except Exception as e:
            LOG.error("Failed to delete Local Extranet policy: %s", e)
            raise ConfigurationError(f"Local Extranet policy deletion failed: {e}")

    def get_policies_summary(self) -> Dict[str, Any]:
        """
        Get summary of all Local Extranet policies.

        Returns:
            dict: {"policies": [...]} summary
        """
        try:
            LOG.info("LocalExtranetManager: Current enterprise info: %s", self.gsdk.enterprise_info)
            LOG.info("Retrieving Local Extranet policies summary")
            policies = self.gsdk.get_local_extranet_policies()

            summary = []
            for policy in policies:
                shared_segment = getattr(policy, "shared_segment", None)
                target_segments = getattr(policy, "target_segments", None) or []
                summary.append(
                    {
                        "id": policy.id,
                        "name": policy.name,
                        "sharedSegment": getattr(shared_segment, "name", None),
                        "targetSegmentsCount": len(target_segments),
                    }
                )

            if summary and HAS_TABULATE:
                LOG.info(
                    "Local Extranet Policies Summary:\n%s",
                    tabulate(
                        [[s["id"], s["name"], s["sharedSegment"], s["targetSegmentsCount"]] for s in summary],
                        headers=["ID", "Name", "Shared Segment", "Target Segments"],
                        tablefmt="grid",
                    ),
                )

            return {"policies": summary}
        except Exception as e:
            LOG.error("Failed to retrieve Local Extranet policies summary: %s", e)
            raise ConfigurationError(f"Failed to retrieve Local Extranet policies summary: {e}")

    def get_policy_by_name(self, policy_name: str) -> Optional[Dict[str, Any]]:
        """
        Get a specific Local Extranet policy by name.

        Args:
            policy_name (str): Name of the policy to retrieve

        Returns:
            dict or None: Policy details if found, None otherwise
        """
        try:
            LOG.info("Retrieving Local Extranet policy '%s'", policy_name)
            policy = self.gsdk.get_local_extranet_policy_by_name(policy_name)
            if not policy:
                return None
            return self.gsdk.get_local_extranet_policy_details(policy.id)
        except Exception as e:
            LOG.error("Failed to retrieve policy '%s': %s", policy_name, e)
            raise ConfigurationError(f"Failed to retrieve policy '{policy_name}': {e}")

    def get_device_status(self, policy_name: str) -> Dict[str, Any]:
        """
        Get per-device push/rollout status for a Local Extranet policy.

        Args:
            policy_name (str): Name of the policy

        Returns:
            dict: {"policy_name", "devices": [...]}
        """
        try:
            policy = self.gsdk.get_local_extranet_policy_by_name(policy_name)
            if not policy:
                raise ConfigurationError(f"Policy '{policy_name}' not found.")

            devices = self.gsdk.get_local_extranet_policy_device_status(policy.id)
            device_rows = [
                {
                    "deviceId": getattr(d, "device_id", None),
                    "deviceName": getattr(d, "device_name", None),
                    "siteName": getattr(d, "site_name", None),
                    "status": getattr(d, "status", None),
                }
                for d in devices
            ]

            if device_rows and HAS_TABULATE:
                LOG.info(
                    "Local Extranet Policy '%s' Device Status:\n%s",
                    policy_name,
                    tabulate(
                        [[d["deviceId"], d["deviceName"], d["siteName"], d["status"]] for d in device_rows],
                        headers=["Device ID", "Device Name", "Site Name", "Status"],
                        tablefmt="grid",
                    ),
                )

            return {"policy_name": policy_name, "devices": device_rows}
        except ConfigurationError:
            raise
        except Exception as e:
            LOG.error("Failed to retrieve device status for policy '%s': %s", policy_name, e)
            raise ConfigurationError(f"Failed to retrieve device status for policy '{policy_name}': {e}")

    def get_lan_segments_usage(self, policy_name: Optional[str] = None, is_provider: Optional[bool] = None) -> dict:
        """
        Get LAN segment usage/monitoring info for Local Extranet.

        Args:
            policy_name (str, optional): Policy name to filter by.
            is_provider (bool, optional): Provider vs consumer view.

        Returns:
            dict: Usage response.
        """
        try:
            policy_id = None
            if policy_name:
                policy = self.gsdk.get_local_extranet_policy_by_name(policy_name)
                if not policy:
                    raise ConfigurationError(f"Policy '{policy_name}' not found.")
                policy_id = policy.id

            response = self.gsdk.get_local_extranet_lan_segments_usage(policy_id, is_provider)
            return response.model_dump(by_alias=True, exclude_none=True) if hasattr(response, "model_dump") else {}
        except ConfigurationError:
            raise
        except Exception as e:
            LOG.error("Failed to retrieve LAN segment usage: %s", e)
            raise ConfigurationError(f"Failed to retrieve LAN segment usage: {e}")

    def get_nat_usage(self, policy_name: str) -> dict:
        """
        Get NAT pool usage/monitoring info for a Local Extranet policy.

        Args:
            policy_name (str): Policy name.

        Returns:
            dict: NAT usage response.
        """
        try:
            policy = self.gsdk.get_local_extranet_policy_by_name(policy_name)
            if not policy:
                raise ConfigurationError(f"Policy '{policy_name}' not found.")

            response = self.gsdk.get_local_extranet_nat_usage(policy.id)
            return response.model_dump(by_alias=True, exclude_none=True) if hasattr(response, "model_dump") else {}
        except ConfigurationError:
            raise
        except Exception as e:
            LOG.error("Failed to retrieve NAT usage for policy '%s': %s", policy_name, e)
            raise ConfigurationError(f"Failed to retrieve NAT usage for policy '{policy_name}': {e}")

    # --- Internal helpers ---

    def _resolve_policy_target_ids(self, target_config: Optional[dict], policy_name: str, target_label: str) -> None:
        """
        Resolve site/device names to IDs in a policy 'source' or 'branches' block (modified
        in place), and convert ``prefixSet.entries`` (if present) from the user-friendly list
        shape to the API's wire shape (see _build_prefix_set_entries).

        ``excludedDevices`` is always set (defaulting to an empty list) even when the config
        doesn't specify one — the API expects the key to be present.

        Args:
            target_config (dict): The 'source' or 'branches' block, or None.
            policy_name (str): Policy name, for error reporting.
            target_label (str): "source" or "branches", for error reporting.
        """
        if not isinstance(target_config, dict):
            return

        site_names = target_config.get("sites")
        if isinstance(site_names, list):
            resolved_sites = []
            for site_name in site_names:
                if isinstance(site_name, str):
                    resolved_sites.append(self.get_site_id(site_name))
                else:
                    resolved_sites.append(site_name)  # Already an ID
            target_config["sites"] = resolved_sites

        resolved_devices = []
        for device_name in target_config.get("excludedDevices") or []:
            if isinstance(device_name, str):
                resolved_devices.append(self.get_device_id(device_name))
            else:
                resolved_devices.append(device_name)  # Already an ID
        target_config["excludedDevices"] = resolved_devices

        prefix_set = target_config.get("prefixSet")
        if isinstance(prefix_set, dict) and isinstance(prefix_set.get("entries"), list):
            prefix_set["entries"] = self._build_prefix_set_entries(
                prefix_set["entries"], policy_name, f"{target_label}.prefixSet"
            )

    def _resolve_policy_ids(self, api_policy: dict, policy_name: str, for_update: bool = False) -> None:
        """
        Resolve all name references in a policy config to IDs (modified in place):
        sharedSegment/targetSegments (LAN segments) and source/branches sites/excludedDevices.

        ``type`` is not user-configurable (the portal UI doesn't expose it), and the value
        that works differs by operation — confirmed against live create/update captures:
          - create_policies (``for_update=False``): sent as the string ``"enterprise"``, via
            the typed create SDK call (``StrictStr``-constrained). A bare numeric ``"type": 2``
            (what the portal's own create request showed) coerced to string ``"2"`` also let
            create succeed, but produced a policy invisible to GET's ``?type=enterprise``
            list filter (what the portal UI's list view calls) — "enterprise" avoids that.
          - update_policies (``for_update=True``): sent as the Python int ``2`` (a real JSON
            integer, via a raw call in ``edit_local_extranet_policy`` — the typed update SDK
            call can only send ``type`` as a string or omit it, and a live capture proved
            those both fail with a backend foreign-key constraint violation
            (``extranet_policy_type_fkey``); only the portal UI's own raw ``"type": 2`` int
            succeeded).

        Args:
            api_policy (dict): Policy configuration to resolve.
            policy_name (str): Policy name, for error reporting.
            for_update (bool): False for create_policies (default), True for update_policies
                — see above for what value each sends.

        Raises:
            ConfigurationError: If a referenced LAN segment name cannot be found.
        """
        api_policy["type"] = 2 if for_update else "enterprise"

        if "sharedSegment" in api_policy and isinstance(api_policy["sharedSegment"], str):
            lan_segment_name = api_policy["sharedSegment"]
            lan_segment_id = self.gsdk.get_lan_segment_id(lan_segment_name)
            if not lan_segment_id:
                raise ConfigurationError(f"LAN segment '{lan_segment_name}' not found for policy '{policy_name}'.")
            api_policy["sharedSegment"] = lan_segment_id

        target_segments = api_policy.get("targetSegments")
        if isinstance(target_segments, list):
            resolved_segments = []
            for segment_name in target_segments:
                if isinstance(segment_name, str):
                    segment_id = self.gsdk.get_lan_segment_id(segment_name)
                    if not segment_id:
                        raise ConfigurationError(
                            f"LAN segment '{segment_name}' not found for policy '{policy_name}' (targetSegments)."
                        )
                    resolved_segments.append(segment_id)
                else:
                    resolved_segments.append(segment_name)  # Already an ID
            api_policy["targetSegments"] = resolved_segments

        self._resolve_policy_target_ids(api_policy.get("source"), policy_name, "source")
        self._resolve_policy_target_ids(api_policy.get("branches"), policy_name, "branches")

    def _resolve_device_names(self, device_names: Optional[list], policy_name: str) -> Optional[List[int]]:
        """
        Resolve a list of device names (the config-only 'targetDevices' key, used solely for
        the apply/device-push step, not part of the API's 'policy' object) to device IDs.

        Args:
            device_names (list, optional): Device names (or already-resolved IDs).
            policy_name (str): Policy name, for error reporting.

        Returns:
            list[int] or None: Resolved device IDs, or None if device_names was empty/omitted
                (apply then pushes to all applicable devices).
        """
        if not device_names:
            return None
        resolved: List[int] = []
        for device_name in device_names:
            if isinstance(device_name, str):
                # get_device_id() raises DeviceNotFoundError rather than returning None.
                resolved.append(cast(int, self.get_device_id(device_name)))
            else:
                resolved.append(device_name)  # Already an ID
        return resolved

    def _build_prefix_set_entries(self, entries: list, policy_name: str, context: str) -> dict:
        """
        Convert a user-friendly list of ``{ipPrefix, maskLower, maskUpper}`` dicts into the
        API's wire shape: ``entries`` keyed by string sequence number, each with an
        auto-assigned ``seq``.

        Defaults mirror the portal UI's "Rule" dropdown (Exact/Range/Less & Equal/Greater &
        Equal), confirmed against a live POST/GET capture — the default is conditional on
        which of maskLower/maskUpper are given, not a flat default for each:
          - Neither given ("Exact"): maskLower = maskUpper = ipPrefix's own mask length.
          - Only maskUpper given ("Less & Equal"): maskLower defaults to ipPrefix's own mask
            length.
          - Only maskLower given ("Greater & Equal"): maskUpper defaults to 32.
          - Both given ("Range"): used as-is.

        Args:
            entries (list): User-supplied prefix entries, e.g.
                ``[{"ipPrefix": "10.1.1.0/24", "maskLower": 25, "maskUpper": 28}]``.
            policy_name (str): Policy name, for error reporting.
            context (str): Where these entries came from (e.g. "source.prefixSet").

        Returns:
            dict: Wire-shaped entries, e.g. ``{"1": {"seq": 1, "ipPrefix": ..., "maskLower":
                ..., "maskUpper": ...}}``.
        """
        wire_entries = {}
        for idx, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict) or "ipPrefix" not in entry:
                raise ConfigurationError(f"Policy '{policy_name}': each {context} entry must have 'ipPrefix'.")
            ip_prefix = entry["ipPrefix"]
            self._validate_cidr_prefixes([ip_prefix], policy_name, context)
            own_mask_length = ipaddress.ip_network(ip_prefix, strict=True).prefixlen

            has_mask_lower = "maskLower" in entry
            has_mask_upper = "maskUpper" in entry
            mask_lower = entry.get("maskLower", own_mask_length)
            if has_mask_upper:
                mask_upper = entry["maskUpper"]
            elif has_mask_lower:
                mask_upper = 32
            else:
                mask_upper = own_mask_length

            wire_entries[str(idx)] = {
                "seq": idx,
                "ipPrefix": ip_prefix,
                "maskLower": mask_lower,
                "maskUpper": mask_upper,
            }
        return wire_entries

    @staticmethod
    def _carry_over_prefix_set_id(desired_target_config: Optional[dict], current_target_config: dict) -> None:
        """
        Carry the existing prefixSet's ``id`` (as returned by GET) into the desired update
        payload for the same source/branches block, unless the desired config already
        specifies one (modifies ``desired_target_config`` in place).

        Without this, a PUT whose ``prefixSet.name`` matches an already-existing prefix-set
        object — but omits ``id`` — is treated by the backend as an attempt to *create* a new
        prefix-set object with that name, which fails with "Prefix-set already exists"
        (confirmed via a live update). The current object's ``id`` must be echoed back to
        update it in place instead.

        Args:
            desired_target_config (dict): The desired 'source' or 'branches' block being
                built for the PUT payload, or None.
            current_target_config (dict): The corresponding 'source'/'branches' block from
                get_local_extranet_policy_details() (expanded read-side shape).
        """
        if not isinstance(desired_target_config, dict):
            return
        desired_prefix_set = desired_target_config.get("prefixSet")
        current_prefix_set = current_target_config.get("prefixSet")
        if not isinstance(desired_prefix_set, dict) or not isinstance(current_prefix_set, dict):
            return
        if "id" not in desired_prefix_set and current_prefix_set.get("id") is not None:
            desired_prefix_set["id"] = current_prefix_set["id"]

    @staticmethod
    def _validate_cidr_prefixes(prefixes: list, policy_name: str, context: str) -> None:
        """
        Validate that each prefix is a properly-aligned CIDR network address (host bits
        zero), matching the portal UI's own validation (see
        DataExchangeManager._validate_cidr_prefixes for the equivalent Data Exchange check).

        Args:
            prefixes (list): Prefix strings to validate.
            policy_name (str): Policy name, for error reporting.
            context (str): Where these prefixes came from (e.g. "manual.prefixes").
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
                    f"Policy '{policy_name}': invalid {context} prefix '{prefix}'. Please make sure the "
                    f"network address of the CIDR is provided{hint}."
                )

    def _validate_policy_prefixes(self, api_policy: dict, policy_name: str) -> None:
        """
        Validate manual.prefixes are properly aligned CIDR network addresses.

        Args:
            api_policy (dict): Resolved policy configuration.
            policy_name (str): Policy name, for error reporting.
        """
        manual_config = api_policy.get("manual")
        if isinstance(manual_config, dict):
            self._validate_cidr_prefixes(manual_config.get("prefixes") or [], policy_name, "manual.prefixes")

    @staticmethod
    def _normalize_prefix_set(prefix_set: Optional[dict]) -> dict:
        """
        Normalize a 'prefixSet' block for idempotency comparison. ``entries`` is a dict
        keyed by string sequence number on the desired/write side (after
        _build_prefix_set_entries) but a plain list on the current/read side (GET) — both
        are reduced here to a sorted list of {ipPrefix, maskLower, maskUpper}, applying the
        same conditional maskLower/maskUpper defaults as _build_prefix_set_entries, so an
        entry the API echoes back without them still compares equal to one that set them
        explicitly.
        """
        if not isinstance(prefix_set, dict):
            return {}

        entries = prefix_set.get("entries")
        raw_entries: List[Any]
        if isinstance(entries, dict):
            raw_entries = list(entries.values())
        elif isinstance(entries, list):
            raw_entries = entries
        else:
            raw_entries = []

        normalized_entries = []
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            ip_prefix = entry.get("ipPrefix")
            try:
                own_mask_length = ipaddress.ip_network(ip_prefix, strict=False).prefixlen if ip_prefix else None
            except ValueError:
                own_mask_length = None

            has_mask_lower = "maskLower" in entry
            has_mask_upper = "maskUpper" in entry
            mask_lower = entry.get("maskLower", own_mask_length)
            if has_mask_upper:
                mask_upper = entry["maskUpper"]
            elif has_mask_lower:
                mask_upper = 32
            else:
                mask_upper = own_mask_length

            normalized_entries.append({"ipPrefix": ip_prefix, "maskLower": mask_lower, "maskUpper": mask_upper})
        normalized_entries.sort(
            key=lambda e: (e.get("ipPrefix") or "", e.get("maskLower") or 0, e.get("maskUpper") or 0)
        )

        return {
            "name": prefix_set.get("name") or "",
            "mode": prefix_set.get("mode") or "",
            "entries": normalized_entries,
        }

    def _normalize_policy_target(self, target_config: Optional[dict]) -> dict:
        """Normalize a resolved 'source'/'branches' block (already-expanded read-side objects
        are reduced to bare IDs) for idempotency comparison."""
        if not isinstance(target_config, dict):
            return {}

        def _as_ids(entries):
            ids = []
            for entry in entries or []:
                if isinstance(entry, dict):
                    ids.append(entry.get("id"))
                elif hasattr(entry, "id"):
                    ids.append(entry.id)
                else:
                    ids.append(entry)
            return sorted([i for i in ids if i is not None])

        return {
            "sites": _as_ids(target_config.get("sites")),
            "excludedDevices": _as_ids(target_config.get("excludedDevices")),
            "prefixSet": self._normalize_prefix_set(target_config.get("prefixSet")),
        }

    def _normalize_policy(self, policy_config: dict) -> dict:
        """
        Normalize a policy dict (either the resolved-IDs desired config, or the
        expanded-objects current state from get_local_extranet_policy_details) into a
        comparable shape for idempotency checks.
        """
        if not isinstance(policy_config, dict):
            return {}

        shared_segment = policy_config.get("sharedSegment")
        if isinstance(shared_segment, dict):
            shared_segment = shared_segment.get("id")

        target_segments = policy_config.get("targetSegments") or []
        target_segment_ids_raw = [(seg.get("id") if isinstance(seg, dict) else seg) for seg in target_segments]
        target_segment_ids = sorted([i for i in target_segment_ids_raw if i is not None])

        return {
            "description": policy_config.get("description") or "",
            "sharedSegment": shared_segment,
            "targetSegments": target_segment_ids,
            "source": self._normalize_policy_target(policy_config.get("source")),
            "branches": self._normalize_policy_target(policy_config.get("branches")),
            "manual": {"prefixes": sorted((policy_config.get("manual") or {}).get("prefixes") or [])},
        }
