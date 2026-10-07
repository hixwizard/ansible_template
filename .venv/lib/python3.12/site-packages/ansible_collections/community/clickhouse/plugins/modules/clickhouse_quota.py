#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright: (c) 2025, John Garland (@johnnyg) <johnnybg@gmail.com>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: clickhouse_quota

short_description: Creates or removes a ClickHouse quota

description:
  - Creates or removes a ClickHouse quota.

attributes:
  check_mode:
    description: Supports check_mode.
    support: full
  idempotent:
    description: At second run will not change anything.
    support: full

version_added: '1.1.0'

author:
  - John Garland (@johnnyg)
  - Rafal Kozlowski (@rkozlo)

extends_documentation_fragment:
  - community.clickhouse.client_inst_opts
  - community.clickhouse.cluster_inst_opts

options:
  state:
    description:
      - Quota state.
      - V(present) creates the quota if it does not exist.
      - V(absent) deletes the quota if it exists.
    type: str
    choices: ['present', 'absent']
    default: 'present'
  name:
    description:
      - Quota name to add or remove.
    type: str
    required: true
  keyed_by:
    description:
      - Keys the quota by the specified key (default is to not key).
    type: str
    choices:
      - user_name
      - ip_address
      - client_key
      - client_key,user_name
      - client_key,ip_address
  limits:
    description:
      - The limits that this quota should enforce.
    type: list
    elements: dict
    suboptions:
      randomized_start:
        description:
          - Whether this interval's start should be randomized.
          - Intervals always start at the same time if not randomized.
        type: bool
        default: false
      interval:
        description:
          - The interval to apply the following quotas on.
          - This is in the format C(<number> <unit>).
          - Where unit is one of second, minute, hour, day, week, month, quarter or year.
        type: str
        required: true
      max:
        description:
          - Maximum values to apply to this interval in this quota.
          - At least one key must be specified.
          - Mutually exclusive with O(limits.no_limits) and O(limits.tracking_only).
        type: dict
        suboptions:
          queries:
            description:
              - Maximum number of queries to enforce in this interval.
            type: int
          query_selects:
            description:
              - Maximum number of query selects to enforce in this interval.
            type: int
          query_inserts:
            description:
              - Maximum number of query inserts to enforce in this interval.
            type: int
          errors:
            description:
              - Maximum number of errors to enforce in this interval.
            type: int
          result_rows:
            description:
              - Maximum number of result rows to enforce in this interval.
            type: int
          result_bytes:
            description:
              - Maximum number of result bytes to enforce in this interval.
            type: int
          read_rows:
            description:
              - Maximum number of rows read to enforce in this interval.
            type: int
          read_bytes:
            description:
              - Maximum number of bytes read to enforce in this interval.
            type: int
          written_bytes:
            description:
              - Maximum number of bytes written to enforce in this interval.
            type: int
          execution_time:
            description:
              - Maximum number of execution time to enforce in this interval.
            type: float
          failed_sequential_authentications:
            description:
              - Maximum number of failed sequential authentications to enforce in this interval.
            type: int
      no_limits:
        description:
          - Don't apply any limits.
          - Mutually exclusive with O(limits.max) and O(limits.tracking_only).
        type: bool
      tracking_only:
        description:
          - Just track usage instead of enforcing.
          - Mutually exclusive with O(limits.max) and O(limits.no_limits).
        type: bool
  apply_to:
    description:
      - Apply this quota to the following list of users/roles dependent on O(apply_to_mode).
      - Can include special keywords of default and current_user or the name of an actual user or role.
      - Is an error to specify this if O(apply_to_mode=all).
    type: list
    elements: str
  apply_to_mode:
    description:
      - When V(listed_only) (default), the quota will only apply to the users/roles specified in O(apply_to).
      - When V(all), the quota will only apply to B(all) users/roles.
      - When V(all_except_listed), the quota will only apply to B(all) the users/roles except those specified in O(apply_to).
    type: str
    choices: ['listed_only', 'all', 'all_except_listed']
    default: 'listed_only'
"""

EXAMPLES = r"""
- name: Create quota
  community.clickhouse.clickhouse_quota:
    name: test_quota
    state: present

- name: Create a quota with limits
  community.clickhouse.clickhouse_quota:
    name: test_quota
    state: present
    limits:
      - interval: 5 minute
        max:
          queries: 100
          execution_time: 100
        apply_to:
          - one_role
          - another_role
    cluster: test_cluster

- name: Remove quota
  community.clickhouse.clickhouse_quota:
    name: test_quota
    state: absent
"""

RETURN = r"""
executed_statements:
  description:
  - Data-modifying executed statements.
  - Interval will be converted and passed to database in seconds.
  returned: on success
  type: list
  sample: ['CREATE QUOTA test_quota']
"""

import copy
import re
from operator import itemgetter

from ansible.module_utils.basic import AnsibleModule
from ansible_collections.community.clickhouse.plugins.module_utils.clickhouse import (
    check_clickhouse_driver,
    client_common_argument_spec,
    connect_to_db_via_client,
    execute_query,
    get_main_conn_kwargs,
    validate_identifier,
    get_on_cluster_clause,
    cluster_argument_spec,
)

_MAX_LIMIT_TYPES = [
    "queries",
    "query_selects",
    "query_inserts",
    "errors",
    "result_rows",
    "result_bytes",
    "read_rows",
    "read_bytes",
    "written_bytes",
    "execution_time",
    "failed_sequential_authentications",
]

_LIMITS_INTERVAL = re.compile(r'^(?P<number>[\d]+(\.[\d]+)?) (?P<unit>second|minute|hour|day|week|month|quarter|year)$', re.IGNORECASE)

_DEFAULT_LIMIT_PARAMS = {
    "randomized_start": False,
    "interval": None,
    "max": {},
    "no_limits": None,
    "tracking_only": None,
}

_DEFAULT_MAX_PARAMS = {
    "queries": None,
    "query_selects": None,
    "query_inserts": None,
    "errors": None,
    "result_rows": None,
    "result_bytes": None,
    "read_rows": None,
    "read_bytes": None,
    "written_bytes": None,
    "execution_time": None,
    "failed_sequential_authentications": None,
}

_DEFAULT_PARAMS = {
    "cluster": None,
    "keyed_by": None,
    "limits": [],
    "apply_to": [],
    "apply_to_mode": "listed_only",
}

executed_statements = []


class ClickHouseQuota:
    def __init__(self, module, client, name):
        validate_identifier(module, name, "quota name")
        self.module = module
        self.client = client
        self.name = name
        self.keyed_by = None
        self.durations = None
        self.apply_to_all = None
        self.apply_to_list = None
        self.apply_to_except = None

        self._exists = None
        self._quota_limits = []
        self._loaded = False

    @property
    def exists(self):
        if self._exists is None:
            query = "SELECT keys, durations, apply_to_all, apply_to_list, apply_to_except FROM system.quotas WHERE name = %(name)s LIMIT 1"
            query_parameters = {'params': {'name': self.name}}
            result = execute_query(self.module, self.client, query, query_parameters)
            if bool(result):
                self._exists = True
                self.keyed_by, self.durations, self.apply_to_all, self.apply_to_list, self.apply_to_except = result[0]
            else:
                self._exists = False
            # We can simply skip later checks here.
            if self.durations == [] and self._exists:
                self._loaded = True
        return self._exists

    @property
    def quota_limits(self):
        if not self._loaded:
            self._load()
        return self._quota_limits

    def _load(self):
        columns = ['duration', 'is_randomized_interval'] + [f'max_{t}' for t in _MAX_LIMIT_TYPES]
        query = f"""SELECT
                        {', '.join(columns)}
                    FROM system.quota_limits
                    WHERE quota_name = %(name)s ORDER BY `duration`"""
        query_parameters = {'params': {'name': self.name}}
        result = execute_query(self.module, self.client, query, query_parameters)

        for entry in result:
            (duration, is_randomized_interval, *max_values) = entry
            max_dict = dict(zip(_MAX_LIMIT_TYPES, max_values))
            limit = {
                'max': max_dict,
                'randomized_start': bool(is_randomized_interval),
                'interval': duration,
            }
            if all(v is None for v in max_dict.values()):
                limit['tracking_only'] = True
            self._quota_limits.append(limit)
        self._loaded = True

    def _build_current_vals_to_compare(self):
        '''Method to replace old parse over show create statement.
        In future probably should be changed if further logic will change.
        '''
        result = {"limits": []}
        if self.quota_limits:
            for entry in self.quota_limits:
                rewritten_limit = {}
                # interval in system.quota_limits is seconds -> represent as "<n> second"
                interval = entry.get("interval")
                rewritten_limit["interval"] = f"{int(interval)} second"

                rewritten_limit["randomized_start"] = bool(entry.get("randomized_start", False))

                # copy max values, keeping only non-None entries
                max_dict = entry.get("max") or {}
                filtered_max = {k: v for k, v in max_dict.items() if v is not None}
                if filtered_max:
                    rewritten_limit["max"] = filtered_max
                else:
                    rewritten_limit["max"] = {}
                    rewritten_limit["tracking_only"] = True

                rewritten_limit["no_limits"] = None

                result["limits"].append(rewritten_limit)

        # keyed_by may be array or string
        if self.keyed_by:
            if isinstance(self.keyed_by, (list, tuple)):
                result["keyed_by"] = ",".join(self.keyed_by)
            else:
                result["keyed_by"] = str(self.keyed_by)

        # apply_to handling: map system columns to apply_to_mode + apply_to list
        if self.apply_to_all and self.apply_to_except:
            result["apply_to_mode"] = "all_except_listed"
            result["apply_to"] = list(self.apply_to_except or [])
        elif self.apply_to_all:
            result["apply_to_mode"] = "all"
        elif self.apply_to_list:
            result["apply_to_mode"] = "listed_only"
            result["apply_to"] = list(self.apply_to_list or [])

        return result

    def _normalize_interval(self, interval):
        '''Normalize passed interval into seconds.'''
        _INTERVAL_CONV = {
            "second": 1,
            "minute": 60,
            "hour": 3600,
            "day": 86400,
            "week": 604800,
            "month": 2629746,
            "quarter": 7889238,
            "year": 31556952,
        }
        match = _LIMITS_INTERVAL.match(interval)
        if not match:
            self.module.fail_json(msg=f"Unexpected interval input {interval}.")
        # Input interval can be fractional. The result clickhouse
        return int(float(match.group('number')) * int(_INTERVAL_CONV[match.group('unit').lower()]))

    def _needs_altering(self):
        """Check if we need to alter to reach desired"""
        if not self.exists:
            return True
        current_params = self._normalize(self._build_current_vals_to_compare())
        desired_params = self._normalize(self.module.params)

        # For debugging version compatibility issues
        if self.module._verbosity >= 3:  # Only show at high verbosity
            self.module.log(f"Current params (normalized): {current_params}")
            self.module.log(f"Desired params (normalized): {desired_params}")

        return current_params != desired_params

    def _do(self, action):
        if action not in ("CREATE", "ALTER"):
            raise ValueError(
                f"Expected action to be CREATE or ALTER but got '{action}'"
            )

        query = " ".join(self._create_sql_clauses(action))

        executed_statements.append(query)

        if not self.module.check_mode:
            execute_query(self.module, self.client, query)

    def create(self):
        """
        Create entity using CREATE X
        Returns whether the entity was created or not
        """
        if self.exists:
            return False

        self._do("CREATE")
        self._exists = True
        return True

    def alter(self):
        """
        Update entity using ALTER X if it needs it
        Returns whether the entity was altered or not
        """
        if not self.exists or not self._needs_altering():
            return False

        self._do("ALTER")
        return True

    def drop(self):
        """Drop entity using DROP X"""
        if not self.exists:
            return False
        cluster = self.module.params['cluster']
        query = f"DROP QUOTA `{self.name}`"
        query += get_on_cluster_clause(self.module, cluster)
        executed_statements.append(query)

        if not self.module.check_mode:
            execute_query(self.module, self.client, query)

        self._exists = False
        return True

    def ensure_state(self):
        state = self.module.params["state"]
        if state not in ("present", "absent"):
            raise ValueError(f"Unexpected state '{state}'")

        if state == "present":
            # create or alter role
            # will do nothing is nothing needs to be done
            changed = self.create() or self.alter()
        else:
            # drop if exists
            changed = self.drop()

        return changed

    def _normalize(self, params):
        normalized = _DEFAULT_PARAMS.copy()
        for key in normalized.keys() & params.keys():
            value = params[key]
            if value is not None:
                if key == "limits":
                    normalized_limits = []
                    for limit_params in value:
                        normalized_limit = _DEFAULT_LIMIT_PARAMS.copy()
                        for limit_key in normalized_limit.keys() & limit_params.keys():
                            limit_value = limit_params[limit_key]
                            if limit_value is not None:
                                if limit_key == 'interval':
                                    limit_value = self._normalize_interval(limit_value)
                                normalized_limit[limit_key] = limit_value
                        normalized_limits.append(normalized_limit)
                    normalized[key] = normalized_limits
                else:
                    normalized[key] = copy.deepcopy(value)
        keyed_by = normalized["keyed_by"]
        if keyed_by:
            normalized["keyed_by"] = ",".join(
                key.strip() for key in keyed_by.split(",")
            )
        if (
            normalized["apply_to_mode"] == "all_except_listed"
            and not normalized["apply_to"]
        ):
            normalized["apply_to_mode"] = "all"
        # no limits is the default so they automatically get removed
        normalized["limits"] = [
            limit for limit in normalized["limits"] if not limit.get("no_limits")
        ]
        for limit in normalized["limits"]:
            max_limit = limit["max"]
            if max_limit:
                limit["max"] = _DEFAULT_MAX_PARAMS | max_limit
        normalized["limits"].sort(key=itemgetter("interval"))
        normalized["apply_to"].sort()
        return normalized

    def _create_sql_clauses(self, action):
        sql_clauses = [f"{action} QUOTA `{self.name}`"]

        cluster = self.module.params["cluster"]
        if cluster:
            sql_clauses.append(get_on_cluster_clause(self.module, cluster).lstrip())

        keyed_by = self.module.params.get("keyed_by")
        if keyed_by:
            sql_clauses.append(f"KEYED BY {keyed_by}")

        limits_sql_clauses = []
        for limit in self.module.params["limits"] or []:
            sql_clause = ["FOR"]
            if limit.get("randomized_start", False):
                sql_clause.append("RANDOMIZED")
            normalized_interval = self._normalize_interval(limit['interval'])
            sql_clause.append(f"INTERVAL {normalized_interval} second")
            max_limits = {
                key: value
                for key, value in (limit.get("max") or {}).items()
                if value is not None
            }
            if max_limits:
                sql_clause.append("MAX")
                sql_clause.append(
                    ", ".join(f"{key} = {value}" for key, value in max_limits.items())
                )
            elif limit.get("no_limits"):
                sql_clause.append("NO LIMITS")
            elif limit.get("tracking_only"):
                sql_clause.append("TRACKING ONLY")
            else:
                raise ValueError(
                    "One of max or no_limits or tracking_only needs to specified"
                )
            limits_sql_clauses.append(" ".join(sql_clause))
        if limits_sql_clauses:
            sql_clauses.append(", ".join(limits_sql_clauses))

        apply_to = self.module.params.get("apply_to", [])
        apply_to_mode = self.module.params["apply_to_mode"]
        if apply_to_mode == "all_except_listed" and not apply_to:
            apply_to_mode = "all"
        if apply_to and apply_to_mode == "all":
            raise ValueError(
                "Cannot specify list of user/roles to apply to when apply_to_mode == all"
            )
        if apply_to_mode == "all":
            sql_clauses.append("TO ALL")
        elif apply_to:
            sql_clauses.append("TO")
            if apply_to_mode == "all_except_listed":
                sql_clauses.append("ALL EXCEPT")
            sql_clauses.append(", ".join(apply_to))

        return sql_clauses


def main():
    # Set up arguments.
    # If there are common arguments shared across several modules,
    # create the common_argument_spec() function under plugins/module_utils/*
    # and invoke here to return a dict with those arguments
    argument_spec = client_common_argument_spec()
    argument_spec.update(
        state=dict(type="str", choices=["present", "absent"], default="present"),
        name=dict(type="str", required=True),
        keyed_by=dict(
            type="str",
            choices=[
                "user_name",
                "ip_address",
                "client_key",
                "client_key,user_name",
                "client_key,ip_address",
            ],
        ),
        limits=dict(
            type="list",
            elements="dict",
            options=dict(
                randomized_start=dict(type="bool", default=False),
                interval=dict(type="str", required=True),
                max=dict(
                    type="dict",
                    options=dict(
                        queries=dict(type="int"),
                        query_selects=dict(type="int"),
                        query_inserts=dict(type="int"),
                        errors=dict(type="int"),
                        result_rows=dict(type="int"),
                        result_bytes=dict(type="int"),
                        read_rows=dict(type="int"),
                        read_bytes=dict(type="int"),
                        written_bytes=dict(type="int"),
                        execution_time=dict(type="float"),
                        failed_sequential_authentications=dict(type="int"),
                    ),
                    required_one_of=[
                        (
                            "queries",
                            "query_selects",
                            "query_inserts",
                            "errors",
                            "result_rows",
                            "result_bytes",
                            "read_rows",
                            "read_bytes",
                            "written_bytes",
                            "execution_time",
                            "failed_sequential_authentications",
                        )
                    ],
                ),
                no_limits=dict(type="bool"),
                tracking_only=dict(type="bool"),
            ),
            mutually_exclusive=[("max", "no_limits", "tracking_only")],
            required_one_of=[("max", "no_limits", "tracking_only")],
        ),
        apply_to=dict(type="list", elements="str"),
        apply_to_mode=dict(
            type="str",
            choices=["listed_only", "all", "all_except_listed"],
            default="listed_only",
        ),
    )

    argument_spec.update(cluster_argument_spec())

    # Instantiate an object of module class
    module = AnsibleModule(
        argument_spec=argument_spec,
        supports_check_mode=True,
    )

    # Assign passed options to variables
    client_kwargs = module.params["client_kwargs"]
    # The reason why these arguments are separate from client_kwargs
    # is that we need to protect some sensitive data like passwords passed
    # to the module from logging (see the arguments above with no_log=True);
    # Such data must be passed as module arguments (not nested deep in values).
    main_conn_kwargs = get_main_conn_kwargs(module)
    name = module.params["name"]

    # Will fail if no driver informing the user
    check_clickhouse_driver(module)

    # Connect to DB
    client = connect_to_db_via_client(module, main_conn_kwargs, client_kwargs)

    # Do the job
    quota = ClickHouseQuota(module, client, name)
    changed = quota.ensure_state()

    # Close connection
    client.disconnect_connection()

    # Users will get this in JSON output after execution
    module.exit_json(changed=changed, executed_statements=executed_statements)


if __name__ == "__main__":
    main()
