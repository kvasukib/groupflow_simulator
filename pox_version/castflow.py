#!/usr/bin/python
# -*- coding: utf-8 -*-

'''
A POX module implementation of the CastFlow clean slate multicast proposal.

Implementation adapted from NOX-Classic CastFlow implementation provided by caioviel.

Depends on openflow.discovery

WARNING: This module is not complete, and should currently only be tested on loop free topologies

Created on July 16, 2013
@author: alexcraig
'''

from collections import defaultdict
from heapq import heapify, heappop, heappush

# POX dependencies
from pox.openflow.discovery import Discovery
from pox.core import core
from pox.lib.revent import *
from pox.lib.util import dpid_to_str
import pox.lib.packet as pkt
from pox.lib.packet.igmp import *   # Required for various IGMP variable constants
from pox.lib.packet.ethernet import *
import pox.openflow.libopenflow_01 as of
from pox.lib.addresses import IPAddr, EthAddr
from pox.lib.recoco import Timer
import time

log = core.getLogger()

OFPP_LOCAL = 65534

class MulticastMembershipRecord:
    """Class representing the groud record state maintained by an IGMPv3 multicast router

    Multicast routers implementing IGMPv3 keep state per group per
    attached network.  This group state consists of a filter-mode, a list
    of sources, and various timers.  For each attached network running
    IGMP, a multicast router records the desired reception state for that
    network.  That state conceptually consists of a set of records of the
    form:

        (multicast address, group timer, filter-mode, (source records))

    Each source record is of the form:

        (source address, source timer)
    """
    
    def __init__(self):
        self.multicast_address = None
        self.group_timer = None
        self.filter_mode = MODE_IS_INCLUDE  # TODO - Re-examine this as the default
        source_records = [] # Source records are stored as a tuple of the source address and the source timer
        self.active_senders = []    # Tuples of (Receiving IP, Adjacent Router)
        self.active_receivers = []  # Tuples of (Sending IP, Adjacent Router)

        
class Router(EventMixin):
    """
    Class representing an OpenFlow router
    """

    def __init__(self):
        self.connection = None
        self.ports = None # Complete list of all ports on the router, contains complete port object provided by connection objects
        self.igmp_ports = [] # List of ports over which IGMP service should be provided, contains only integer indexes
        self.dpid = None
        self._listeners = None
        self._connected_at = None
        
        # Dictionary of connected host IPs keyed by port numbers
        self.connected_hosts = defaultdict(lambda : None)

    def __repr__(self):
        return dpid_to_str(self.dpid)

    def disconnect(self):
        if self.connection is not None:
            log.debug('Disconnect %s' % (self.connection, ))
            self.connection.removeListeners(self._listeners)
            self.connection = None
            self._listeners = None

    def connect(self, connection):
        if self.dpid is None:
            self.dpid = connection.dpid
        assert self.dpid == connection.dpid
        if self.ports is None:
            self.ports = connection.features.ports
            for port in self.ports:
                if port.port_no ==  of.OFPP_CONTROLLER or port.port_no == of.OFPP_LOCAL:
                    continue
                self.igmp_ports.append(port.port_no)
            # log.debug(self.ports) # Debug
        self.disconnect()
        log.debug('Connect %s' % (connection, ))
        self.connection = connection
        self._listeners = self.listenTo(connection)
        self._connected_at = time.time()

    @property
    def is_holding_down(self):
        if self._connected_at is None:
            return True
        if time.time() - self._connected_at > FLOOD_HOLDDOWN:
            return False
        return True

    def _handle_ConnectionDown(self, event):
        self.disconnect()


class CastflowManager(object):

    def __init__(self):
        # Listen to dependencies
        def startup():
            core.openflow.addListeners(self)
            core.openflow_discovery.addListeners(self)
        
        # Set variables for IGMP operation to default values
        self.igmp_robustness = 2
        self.igmp_query_interval = 125               # Seconds
        # Debug: Make the IGMP query interval shorter than default to aid testing
        self.igmp_query_interval = 20
        self.igmp_query_response_interval = 100     # Tenths of a second
        self.igmp_group_membership_interval = (self.igmp_robustness * self.igmp_query_interval) \
                + (self.igmp_query_response_interval * 0.1)             # Seconds
        # Note: Other querier present interval should not actually be required with this centralized
        # implementation
        self.igmp_other_querier_present_interval = (self.igmp_robustness * self.igmp_query_interval) \
                + ((self.igmp_query_response_interval * 0.1) / 2)       # Seconds
        self.igmp_startup_query_interval = self.igmp_query_interval / 4  # Seconds
        self.igmp_startup_query_count = self.igmp_robustness
        self.igmp_last_member_query_interval = 10                       # Tenths of a second
        self.igmp_last_member_query_count = self.igmp_robustness
        self.igmp_last_member_query_time = self.igmp_last_member_query_interval \
                * self.igmp_last_member_query_count                     # Tenths of a second
        self.igmp_unsolicited_report_interval = 1       # Seconds (not used in router implementation)

        # Setup topology discovery state
        self.got_first_connection_up = False
        
        # Known routers:  [dpid] -> Router
        self.routers = {}
        # Adjacency map:  [router1][router2] -> port from router1 to router2
        self.adjacency = defaultdict(lambda : defaultdict(lambda : \
                None))
        
        # Setup IGMP state
        self.membership_records = defaultdict(lambda : None)    # Group records are keyed by the multicast group address

        # Setup listeners
        core.call_when_ready(startup, ('openflow', 'openflow_discovery'))
        
    def launch_igmp_general_query(self):
        """Generates an IGMP general query, broadcasts it from all ports on all routers
        
        TODO: This could be improved by only broadcasting the query on ports which are not
        connected to an adjacent router in the same control domain, as these queries will
        be discarded by the adjacent routers anyway.
        """
        log.debug('Launching IGMP general query from all routers')
        
        # Build the IGMP payload for the message
        igmp_pkt = pkt.igmp()
        igmp_pkt.ver_and_type = MEMBERSHIP_QUERY
        igmp_pkt.max_response_time = self.igmp_query_response_interval
        igmp_pkt.address = IPAddr("0.0.0.0")
        igmp_pkt.qrv = self.igmp_robustness
        igmp_pkt.qqic = self.igmp_query_interval
        igmp_pkt.dlen = 12  # TODO: This should be determined by the IGMP packet class somehow
        
        # Build the encapsulating IP packet
        ip_pkt = pkt.ipv4()
        ip_pkt.ttl = 1
        ip_pkt.protocol = IGMP_PROTOCOL
        ip_pkt.tos = 0xc0   # Type of service: Internetwork Control
        ip_pkt.dstip = IGMP_V3_ALL_SYSTEMS_ADDRESS
        # TODO: Set IP Router alert option
        ip_pkt.payload = igmp_pkt
        
        # Build the encapsulating ethernet packet
        eth_pkt = eth = pkt.ethernet(type=pkt.ethernet.IP_TYPE)
        eth_pkt.dst = ETHER_BROADCAST
        eth_pkt.payload = ip_pkt
        
        # Send out the packet
        for router_dpid in self.routers:
            sending_router = self.routers[router_dpid]
            # for neighbour in self.adjacency[sending_router]:  # Debug, send to neighbouring routers only
            #    port_num = self.adjacency[sending_router][neighbour]
            for port_num in sending_router.igmp_ports:
                output = of.ofp_packet_out(action = of.ofp_action_output(port=port_num))
                output.data = eth_pkt.pack()
                output.pack()
                sending_router.connection.send(output)
                # log.debug('Router ' + str(sending_router) + ' sending IGMP query on port: ' + str(port_num))
        
        
    def drop_packet(self, packet_in_event):
        """Drops the packet represented by the PacketInEvent without any flow table modification"""
        msg = of.ofp_packet_out()
        msg.data = packet_in_event.ofp
        msg.buffer_id = packet_in_event.ofp.buffer_id
        msg.in_port = packet_in_event.port
        msg.actions = []    # No actions = drop packet
        packet_in_event.connection.send(msg)
        log.debug('Packet dropped')

    def _handle_LinkEvent(self, event):
        """Handler for LinkEvents from the discovery module, which are used to learn the network topology."""

        def flip(link):
            return Discovery.Link(link[2], link[3], link[0], link[1])

        # log.info('Handling LinkEvent: ' + str(event.link)
        #          + ' - State: ' + str(event.added))
        # if event.added:
        #    self.links.append(event.link)
        # else:
        #    self.links.remove(event.link)
        # log.debug(self.links)

        l = event.link
        router1 = self.routers[l.dpid1]
        router2 = self.routers[l.dpid2]

        if event.removed:
            # This link no longer up
            if router2 in self.adjacency[router1]:
                del self.adjacency[router1][router2]
            if router1 in self.adjacency[router2]:
                del self.adjacency[router2][router1]
            router1.igmp_ports.append(l.port1)
            router2.igmp_ports.append(l.port2)
                
            log.info('Removed adjacency: ' + str(router1) + '.'
                 + str(l.port1) + ' <-> ' + str(router2) + '.'
                 + str(l.port2))

            # These routers may still be adjacent through a different link
            for ll in core.openflow_discovery.adjacency:
                if ll.dpid1 == l.dpid1 and ll.dpid2 == l.dpid2:
                    if flip(ll) in core.openflow_discovery.adjacency:
                        # Yup, link goes both ways
                        log.info('Found parallel adjacency');
                        self.adjacency[router1][router2] = ll.port1
                        router1.igmp_ports.remove(ll.port1)
                        self.adjacency[router2][router1] = ll.port2
                        router2.igmp_ports.remove(ll.port2)
                        # Fixed -- new link chosen to connect these
                        break
        else:
            # If we already consider these nodes connected, we can ignore this link up.
            # Otherwise, we might be interested...
            if self.adjacency[router1][router2] is None:
                # These previously weren't connected.  If the link
                # exists in both directions, we consider them connected now.
                if flip(l) in core.openflow_discovery.adjacency:
                    # Link goes both ways -- connected!
                    self.adjacency[router1][router2] = l.port1
                    router1.igmp_ports.remove(l.port1)
                    self.adjacency[router2][router1] = l.port2
                    router2.igmp_ports.remove(l.port2)
                    log.info('Added adjacency: ' + str(router1) + '.'
                             + str(l.port1) + ' <-> ' + str(router2) + '.'
                             + str(l.port2))

                    # Remove router IPs that have been previously identified as hosts
                    if l.port1 in router1.connected_hosts:
                        log.debug('Deleted connected host (' + str(router1.connected_hosts[l.port1]) + ') on port ' + str(l.port1) + ' (host is actually a router)')
                        del router1.connected_hosts[l.port1]
                    if l.port2 in router2.connected_hosts:
                        log.debug('Deleted connected host (' + str(router2.connected_hosts[l.port2]) + ') on port ' + str(l.port2) + ' (host is actually a router)')
                        del router2.connected_hosts[l.port2]

                    # While it is tempting to add a flow table entry to block IGMP messages from being transmitted between
                    # adjoining routers, this would also cause IGMP packets to be dropped silently without processing
                    # by the controller.

    def _handle_ConnectionUp(self, event):
        """Handler for ConnectionUp from the discovery module, which represent new routers joining the network."""
        router = self.routers.get(event.dpid)
        if router is None:
            # New router
            router = Router()
            router.dpid = event.dpid
            self.routers[event.dpid] = router
            log.info('Learned new router: ' + str(router))
            router.connect(event.connection)
        
        if not self.got_first_connection_up:
            self.got_first_connection_up = True
            # Setup the Timer to send periodic general queries
            self.general_query_timer = Timer(self.igmp_query_interval, self.launch_igmp_general_query, recurring = True)
            log.debug('Launching IGMP general query timer with interval ' + str(self.igmp_query_interval) + ' seconds')
            
    def _handle_ConnectionDown (self, event):
        """Handler for ConnectionUp from the discovery module, which represent a router leaving the network."""
        router = self.routers.get(event.dpid)
        if router is None:
            log.warn('Got ConnectionDown for unrecognized router')
        else:
            log.info('Router down: ' + str(self.routers[event.dpid]))
            del self.routers[event.dpid]
    
    def _handle_PacketIn(self, event):
        """Handler for OpenFlow PacketIn events.

        Two types of packets are of primary importance for this module:
        1) IGMP packets: All IGMP packets in the network should be directed to the controller, as the controller
        acts as a single, centralized IGMPv3 multicast router for the purposes of determining group membership.
        2) IP packets destined to a multicast address: When a new multicast flow is received, this should trigger
        a calculation of a multicast tree, and the installation of the appropriate flows in the network.
        """
        # log.debug('Router ' + str(event.connection.dpid) + ':' + str(event.port) + ' PacketIn')
        
        # Make sure this router is known to the controller, ignore the packet if not
        if not event.connection.dpid in self.routers:
            log.debug('Got packet from unrecognized router: ' + str(event.connection.dpid))
            return
        receiving_router = self.routers[event.connection.dpid]
        
        igmp_pkt = event.parsed.find(pkt.igmp)
        if not igmp_pkt is None:
            # ==== IGMP packet - IPv4 Network ====
            # Determine the source IP of the packet
            log.debug(str(receiving_router) + ':' + str(event.port) + '| Received IGMP packet')
            ipv4_pkt = event.parsed.find(pkt.ipv4)
            log.debug(str(receiving_router) + ':' + str(event.port) + '| ' + str(igmp_pkt) + ' from Host: ' + str(ipv4_pkt.srcip))
            
            # Check to see if this IGMP message was received from a neighbouring router, and if so
            # add a rule to drop additional IGMP packets on this port
            for neighbour in self.adjacency[receiving_router]:
                if self.adjacency[receiving_router][neighbour] == event.port:
                    log.debug(str(receiving_router) + ':' + str(event.port) + '| IGMP packet received from neighbouring router.')
                    self.drop_packet(event)
                    
                    # This doesn't appear to actually be working with mininet
                    # msg = of.ofp_flow_mod()
                    # msg.match.dl_type = ipv4_pkt.protocol
                    # msg.match.nw_dst = ipv4_pkt.dstip
                    # msg.match.in_port = event.port
                    ## msg.actions.append(of.ofp_action_output(port = of.OFPP_NONE))
                    # msg.action = []     # No actions = drop packet
                    # event.connection.send(msg)
                    # log.info(str(receiving_router) + ':' + str(event.port) + '| Installed flow to drop all IGMP packets on port: ' + str(event.port))
                    return
            
            # The host must be directly connected to this router (or it is a router in an adjoining
            # network outside the controllers administrative domain), learn its IP and port
            if not event.port in receiving_router.connected_hosts:
                receiving_router.connected_hosts[event.port] = ipv4_pkt.srcip
                log.info('Learned new host (IGMP packet): ' + str(ipv4_pkt.srcip) + ':' + str(event.port))

            # Drop the IGMP packet to prevent it from being uneccesarily forwarded to neighbouring routers
            self.drop_packet(event)
            return
            
        ipv4_pkt = event.parsed.find(pkt.ipv4)
        if not ipv4_pkt is None:
            # ==== IPv4 Packet ====
            # Check the destination address to see if this is a multicast packet
            if ipv4_pkt.dstip.inNetwork('224.0.0.0/4'):
                log.debug(str(receiving_router) + ':' + str(event.port) + '| Received non-IGMP multicast packet')
            
                from_neighbour_router = False
                for neighbour in self.adjacency[receiving_router]:
                    if self.adjacency[receiving_router][neighbour] == event.port:
                        from_neighbour_router = True
                        break
                if not from_neighbour_router:
                    # This packet must be from a connected host (more specifically, a multicast sender)
                    if not event.port in receiving_router.connected_hosts:
                        receiving_router.connected_hosts[event.port] = ipv4_pkt.srcip
                        log.info('Learned new host (multicast packet): ' + str(ipv4_pkt.srcip) + ':' + str(event.port))
                
                # For now, just broadcast all packets from this multicast group
                # TODO: Does a separate ofp_packet_out msg actually need to be send to forward this particular packet?
                msg = of.ofp_packet_out()
                msg.data = event.ofp
                msg.buffer_id = event.ofp.buffer_id
                msg.in_port = event.port
                msg.actions.append(of.ofp_action_output(port = of.OFPP_FLOOD))
                event.connection.send(msg)
        
                msg = of.ofp_flow_mod()
                msg.match.dl_type = 0x800   # IPV4
                msg.match.nw_dst = ipv4_pkt.dstip
                msg.match.in_port = event.port
                msg.actions.append(of.ofp_action_output(port = of.OFPP_FLOOD))
                event.connection.send(msg)
                log.info(str(receiving_router) + ':' + str(event.port) + '| Installed flow to flood all packets for multicast group: ' + str(ipv4_pkt.dstip))
                
        
        
def launch():
    core.registerNew(CastflowManager)


