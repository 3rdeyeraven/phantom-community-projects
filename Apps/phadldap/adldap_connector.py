# File: adldap_connector.py
# Copyright (c) 2017-2019 Splunk Inc.
#
# Licensed under Apache 2.0 (https://www.apache.org/licenses/LICENSE-2.0.txt)


# Phantom App imports
import phantom.app as phantom
# import json
from phantom.base_connector import BaseConnector
from phantom.action_result import ActionResult

# switched from python-ldap to ldap3 for this app. -gsh
import ldap3
import ldap3.extend.microsoft.addMembersToGroups
import ldap3.extend.microsoft.removeMembersFromGroups
import ldap3.extend.microsoft.unlockAccount
from ldap3.utils.dn import parse_dn
import json
# from adldap_consts import *


class RetVal(tuple):
    def __new__(cls, val1, val2=None):
        return tuple.__new__(RetVal, (val1, val2))


class AdLdapConnector(BaseConnector):

    def __init__(self):
        super(AdLdapConnector, self).__init__()

    def _ldap_bind(self, action_result=None):
        """
        returns phantom.APP_SUCCESS if connection succeeded,
        else phantom.APP_ERROR.

        If an action_result is passed in, method will
        appropriately use it. Otherwise just return
        APP_SUCCESS/APP_ERROR
        """
        if self._ldap_connection and \
                self._ldap_connection.bound and \
                not self._ldap_connection.closed:
            return True
        elif self._ldap_connection is not None:
            self._ldap_connection.unbind()

        try:
            server_param = {
                "use_ssl": self._ssl,
                "port": self._ssl_port,
                "host": self._server,
                "get_info": ldap3.ALL
            }

            self._ldap_server = ldap3.Server(**server_param)
            self.save_progress("configured server {}...".format(self._server))
            self._ldap_connection = ldap3.Connection(self._ldap_server,
                                                     user=self._username,
                                                     password=self._password,
                                                     raise_exceptions=True)
            self.save_progress("binding to directory...")

            if not self._ldap_connection.bind():
                if action_result:
                    return action_result.set_status(
                        phantom.APP_ERROR,
                        self._ldap_connection.result['description']
                    )
                else:
                    return phantom.APP_ERROR

            if action_result:
                return action_result.set_status(phantom.APP_SUCCESS)
            else:
                return phantom.APP_SUCCESS

        except Exception as e:
            self.debug_print("[DEBUG] ldap_bind, e = {}".format(e))
            if action_result:
                return action_result.set_status(
                    phantom.APP_ERROR,
                    status_message="",
                    exception=e
                )
            else:
                return phantom.APP_ERROR

    def _get_root_dn(self, action_result=None):
        """
        returns root dn (str) if found, else False.
        """
        if self._ldap_bind():
            try:
                return \
                    self._ldap_connection.server.info.other['defaultNamingContext'][0]
            except Exception:
                return False
        else:
            return False

    def _sam_to_dn(self, sam, action_result=None):
        """
        This method will take a list of samaccountnames
        and return a dictionary with the key as the
        samaccountname and the value as the distinguishedname.

        If a corresponding distinguishedname was not found, then
        the key will be the samaccountname and the value will be
        False.
        """

        # create a usable ldap filter
        filter = "(|"
        for u in sam:
            filter = filter + "(samaccountname={})".format(u)
        filter = filter + ")"
        p = {
            "attributes": "distinguishedname;samaccountname",
            "filter": filter
        }
        dn = json.loads(self._query(param=p))
        r = {name: False for name in sam}
        for i in dn['entries']:
            s = i['attributes']['sAMAccountName']
            if s in r:
                r[s] = i['attributes']['distinguishedName']

        self.debug_print("[DEBUG] sam = {}, len(sam) = {}".format(sam, len(sam)))

        # if action_result, add summary regarding number of records requested
        # vs number of records found.
        if action_result:
            action_result.update_summary({
                "requested records": len(sam),
                "found records": len([k for (k, v) in r.items() if v is not False])
            })
        return r

    def _get_filtered_response(self):
        """
        returns a list of objects from LDAP results
        that do not match type=searchResRef
        """
        try:
            return [i for i in self._ldap_connection.response
                    if i['type'] != 'searchResRef']
        except Exception as e:
            self.debug_print("[DEBUG] get_filtered_response(), exception: {}".format(str(e)))
            return []

    def _handle_group_members(self, param, add):
        """
        handles membership additions and removals.
        if add=True then add to groups.
        if add=False then remove from groups.
        """

        action_result = self.add_action_result(ActionResult(dict(param)))
        if not self._ldap_bind():
            return RetVal(action_result.set_status(phantom.APP_ERROR))

        members = [i.strip() for i in param['members'].split(';')]
        groups = [i.strip() for i in param['groups'].split(';')]

        # resolve samaccountname -> distinguishedname if option selected
        if param.get('use_samaccountname', False):
            n_members = []  # new list of users
            n_groups = []   # new list of groups
            member_nf = []  # not found users
            group_nf = []   # not found groups

            # finding users dn by sam
            t_users = self._sam_to_dn(members, action_result=action_result)
            for k, v in t_users.items():
                if v is False:
                    member_nf.append(k)
                else:
                    n_members.append(v)

            # finding groups dn by sam
            t_group = self._sam_to_dn(groups, action_result=action_result)
            for k, v in t_group.items():
                if v is False:
                    group_nf.append(k)
                else:
                    n_groups.append(v)

            # ensure we actually have a least 1 user and group to modify
            if len(n_members) > 0 and len(n_groups) > 0:
                members = n_members
                groups = n_groups
            else:
                return RetVal(
                    action_result.set_status(phantom.APP_ERROR)
                )
        self.debug_print("[DEBUG] members = {}, groups = {}".format(members, groups))

        try:
            if add:
                func = "added"
                ldap3.extend.microsoft.addMembersToGroups.ad_add_members_to_groups(
                    connection=self._ldap_connection,
                    members_dn=members,
                    groups_dn=groups,
                    fix=True,
                    raise_error=True
                )
            else:
                func = "removed"
                ldap3.extend.microsoft.removeMembersFromGroups.ad_remove_members_from_groups(
                    connection=self._ldap_connection,
                    members_dn=members,
                    groups_dn=groups,
                    fix=True,
                    raise_error=True
                )
        except Exception as e:
            return RetVal(action_result.set_status(
                phantom.APP_ERROR,
                "",
                exception=e
            ))

        # add action data results
        for i in members:
            for j in groups:
                action_result.add_data({
                    "member": i,
                    "group": j,
                    "function": func
                })

        return RetVal(action_result.set_status(
            phantom.APP_SUCCESS,
            "{} member(s) to group(s).".format(func)
        ))

    def _handle_unlock_account(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))
        summary = action_result.update_summary({})

        user = param['user']

        if param.get("use_samaccountname", False):
            user_dn = self._sam_to_dn([user])   # _sam_to_dn requires a list.
            self.debug_print("[DEBUG] handle_unlock_account user_dn = {}".format(user_dn))
            if len(user_dn) == 0 or user_dn[user] is False:
                return RetVal(action_result.set_status(
                    phantom.APP_ERROR
                ))

        if not self._ldap_bind():
            return RetVal(action_result.set_status(phantom.APP_ERROR))

        try:
            ldap3.extend.microsoft.unlockAccount.ad_unlock_account(
                self._ldap_connection,
                user_dn=user_dn[user],
            )
        except Exception as e:
            return RetVal(action_result.set_status(
                phantom.APP_ERROR,
                "",
                exception=e
            ))

        summary['summary'] = "Unlocked"
        return RetVal(action_result.set_status(
            phantom.APP_SUCCESS
        ))

    def _handle_account_status(self, param, disable=False):
        """
        This reads in the existing UAC and _only_ modifies the disabled. Does not
        reset any additional flags.
        """
        action_result = self.add_action_result(ActionResult(dict(param)))

        if not self._ldap_bind():
            return RetVal(action_result.set_status(phantom.APP_ERROR))

        user = param['user']

        try:
            query_params = {
                "attributes": "useraccountcontrol",
                "filter": "(distinguishedname={})".format(user)
            }
            self._handle_query(query_params)
            resp = self._get_filtered_response()
            uac = int(resp[0]['attributes']['userAccountControl'])

            if disable:
                mod_uac = uac | 0x02
            else:
                mod_uac = uac & (0xFFFFFFFF ^ 0x02)
            res = self._ldap_connection.modify(
                user, {'userAccountControl': [
                    (ldap3.MODIFY_REPLACE, [mod_uac])
                ]})
            if not res:
                return RetVal(action_result.set_status(
                    phantom.APP_ERROR,
                    self._ldap_connection.result,
                ))
        except Exception as e:
            self.debug_print("[DEBUG] disable_account error = {}".format(e))
            return RetVal(action_result.set_status(
                phantom.APP_ERROR,
                "",
                exception=e
            ))

        return RetVal(action_result.set_status(
            phantom.APP_SUCCESS
        ))

    def _handle_move_object(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))
        obj = param['object']
        new_ou = param['new_ou']

        if not self._ldap_bind():
            return RetVal(action_result.set_status(phantom.APP_ERROR))

        try:
            cn = '='.join(parse_dn(obj)[0][:-1])
            res = self._ldap_connection.modify_dn(obj, cn, new_superior=new_ou)
            if not res:
                return RetVal(action_result.set_status(
                    phantom.APP_ERROR,
                    self._ldap_connection.result,
                ))
        except Exception as e:
            return RetVal(action_result.set_status(
                phantom.APP_ERROR,
                "",
                exception=e
            ))

    def _handle_test_connectivity(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))

        # failure
        if not self._ldap_bind(action_result):
            self.save_progress("Test Connectivity Failed.")
            return action_result.get_status()

        # success
        self.save_progress("Test Connectivity Passed")
        return action_result.set_status(action_result.get_status())

    def _handle_get_attributes(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))
        summary = action_result.update_summary({})

        if not self._ldap_bind():
            return phantom.APP_ERROR

        query = "(|"
        principal = [i.strip() for i in param['principals'].split(';')]

        # build a query on the fly with the principals provided
        for i in principal:
            query += "(userprincipalname={0})(samaccountname={0})(distinguishedname={0})".format(i)
        query += ")"

        self.debug_print("[DEBUG] handle_get_attributes, query = {}".format(query))

        resp = self._query({"filter": query, "attributes": param['attributes']})
        self.debug_print("[DEBUG] handle_get_attributes, resp = {}".format(json.loads(resp)))

        action_result.add_data(json.loads(resp))
        summary['total_objects'] = len(self._get_filtered_response())
        return RetVal(
            action_result.set_status(
                phantom.APP_SUCCESS
            ))

    def _handle_set_attribute(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))
        # summary = action_result.update_summary({})

        user = param['user']
        attribute = param['attribute']
        value = param['value']
        action = param['action']

        changes = {}

        if action == "ADD":
            changes[attribute] = [(ldap3.MODIFY_ADD, [value])]
        elif action == "DELETE":
            changes[attribute] = [(ldap3.MODIFY_DELETE, [value])]
        elif action == "REPLACE":
            changes[attribute] = [(ldap3.MODIFY_REPLACE, [value])]

        if not self._ldap_bind():
            return phantom.APP_ERROR

        try:
            ret = self._ldap_connection.modify(
                dn=user,
                changes=changes
            )
            self.debug_print("[DEBUG] handle_set_attribute, ret = {}".format(ret))
        except Exception as e:
            RetVal(
                action_result.set_status(
                    phantom.APP_ERROR,
                    "",
                    e)
            )
        action_result.add_data({"modified": ret})
        return RetVal(
            action_result.set_status(phantom.APP_SUCCESS)
        )

    def _query(self, param):
        """
        This method handles the query and returns
        the response in the ldap connectin object.

        Returns the data or throws and exception.
        param must include:
            - attributes (to retrieve - semi-colon separated string:
                e.g. "mail;samaccountname;pwdlastset")
            - filter (ldap query)
        """
        attrs = [i.strip() for i in param['attributes'].split(';')]
        filter = param['filter']
        search_base = param.get('search_base', self._get_root_dn())

        # throw exception if we cannot bind
        if not self._ldap_bind():
            raise Exception(self._ldap_bind.result)

        self._ldap_connection.search(
            search_base=search_base,
            search_filter=filter,
            search_scope=ldap3.SUBTREE,
            attributes=attrs)

        return self._ldap_connection.response_to_json()

    def _handle_query(self, param):
        """
        This method handles arbitrary LDAP queries for
        those who are skilled w/ that syntax.
        e.g.
        "(|(&(mail=alice@company*)(samaccountname=alice*))(manager=bob))
        """

        action_result = self.add_action_result(ActionResult(dict(param)))
        self.save_progress("In action handler for: {0}".format(self.get_action_identifier()))

        summary = action_result.update_summary({})

        try:
            resp = self._query(param)
        except ldap3.LDAPSocketOpenError as e:
            return RetVal(action_result.set_status(
                phantom.APP_ERROR,
                "Invalid server address. Two common causes: invalid Server hostname or search_base"
            ))
        except Exception as e:
            return RetVal(action_result.set_status(
                phantom.APP_ERROR,
                str(e)
            ))

        action_result.add_data(
            json.loads(
                resp
            ))

        # Add a dictionary that is made up of the most important values from data into the summary
        summary['Total Records Found'] = len(self._get_filtered_response())
        return RetVal(action_result.set_status(phantom.APP_SUCCESS))

    def _handle_reset_password(self, param):
        action_result = self.add_action_result(ActionResult(dict(param)))
        self.save_progress("In action handler for: {0}".format(self.get_action_identifier()))
        self.debug_print("[DEBUG] handle_reset_password")

        user = param['user']
        pwd = param['password']

        if not self._ldap_bind():
            self.debug_print("[DEBUG] handle_reset_password - no bind")
            raise Exception(self._ldap_bind.result)

        try:
            ret = self._ldap_connection.extend.microsoft.modify_password(user, pwd)
        except Exception as e:
            self.debug_print("[DEBUG] handle_reset_password, e = {}".format(str(e)))
            return RetVal(
                action_result.set_status(phantom.APP_ERROR),
                "",
                e
            )
        self.debug_print("[DEBUG] handle_reset_password, ret = {}".format(ret))
        if ret:
            action_result.add_data({"reset": True})
            return RetVal(action_result.set_status(phantom.APP_SUCCESS))
        else:
            action_result.add_data({"reset": False})
            return RetVal(action_result.set_status(phantom.APP_ERROR))

    def handle_action(self, param):

        ret_val = phantom.APP_SUCCESS

        # Get the action that we are supposed to execute for this App Run
        action_id = self.get_action_identifier()

        self.debug_print("action_id", self.get_action_identifier())

        if action_id == 'test_connectivity':
            ret_val = self._handle_test_connectivity(param)

        elif action_id == 'query':
            ret_val = self._handle_query(param)

        elif action_id == 'add_group_members':
            ret_val = self._handle_group_members(param, True)

        elif action_id == 'remove_group_members':
            ret_val = self._handle_group_members(param, False)

        elif action_id == 'unlock_account':
            ret_val = self._handle_unlock_account(param)

        elif action_id == 'disable_account':
            ret_val = self._handle_account_status(param, disable=True)

        elif action_id == 'enable_account':
            ret_val = self._handle_account_status(param, disable=False)

        elif action_id == "move_object":
            ret_val = self._handle_move_object(param)

        elif action_id == "get_attributes":
            ret_val = self._handle_get_attributes(param)

        elif action_id == "set_attribute":
            ret_val = self._handle_set_attribute(param)

        elif action_id == "reset_password":
            ret_val = self._handle_reset_password(param)

        return ret_val

    def initialize(self):

        # Load the state in initialize, use it to store data
        # that needs to be accessed across actions
        self._state = self.load_state()

        # get the asset config
        config = self.get_config()

        # load our config for use.
        self._server = config['server']
        self._username = config['username']
        self._password = config['password']
        self._ssl = config['force_ssl']
        self._ssl_port = int(config['ssl_port'])
        self.connected = False
        self._ldap_connection = None

        return phantom.APP_SUCCESS

    def finalize(self):

        # Save the state, this data is saved across actions and app upgrades
        self.save_state(self._state)
        return phantom.APP_SUCCESS
