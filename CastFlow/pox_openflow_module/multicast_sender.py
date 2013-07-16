#!/usr/bin/env python
import socket
import struct
import sys
import threading
import time

# To work in Mininet routes must be configured for hosts similar to the following:
# route add -net 224.0.0.0/4 h1-eth0

quit_flag = False

multicast_group = '224.1.1.1'
multicast_port = 5007
echo_port = 5008
send_socket = None
send_packet_index = 1
send_packet_times = {}
echo_packet_times = {}

def send_multicast_packet():
	global send_packet_index, send_packet_times
	send_string = str(send_packet_index).zfill(512)
	send_packet_times[send_packet_index] = time.time()
	bytes = send_socket.sendto(send_string, (multicast_group, multicast_port))
	# print 'Sent multicast packet ' + str(send_packet_index) + ' at: ' + str(send_packet_times[send_packet_index]) + ' (' + str(bytes) + ' bytes)'
	send_packet_index += 1
	if not quit_flag:
		threading.Timer(1, send_multicast_packet).start()
	
def main():
	global multicast_group, multicast_port, send_socket
	
	if len(sys.argv) > 1:
		multicast_group = sys.argv[1]
	
	if len(sys.argv) > 2:
		multicast_port = sys.argv[2]
	
	send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
	send_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 32)
	
	print 'Starting multicast on group: ' + multicast_group + ':' + str(multicast_port)
	threading.Timer(1, send_multicast_packet).start()
	
	print 'Beginning listening on echo socket'
	echo_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	echo_socket.bind(('', echo_port))
	
	last_echo_index = 1
	while True:
		data, addr = echo_socket.recvfrom(512)
		echo_time = time.time() - send_packet_times[int(data)]
		echo_index = str(int(data))
		if echo_index != last_echo_index:
			print '=================='
		print 'Echo P#: ' + echo_index + '\tHost: ' + str(addr[0]) + '\t Time: ' + "{:0.6f}".format(echo_time * 1000) + ' ms'
		last_echo_index = echo_index

if __name__ == '__main__':
	main()