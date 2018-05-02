// An high-level emulator of HP Amigo drives for use with MAME IEEE-488 remotizer
// Copyright (C) 2018 F. Ulivi <fulivi at big "G" mail>
//
// This program is free software; you can redistribute it and/or
// modify it under the terms of the GNU General Public License
// as published by the Free Software Foundation; either version 2
// of the License, or (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with this program.  If not, see
// <http://www.gnu.org/licenses/>.
#include <iostream>
#include <thread>
#include <condition_variable>
#include <mutex>
#include <vector>
#include <queue>
#include <string>
#include <memory>
#include <map>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include <unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>

// Message types
constexpr char MSG_SIGNAL_CLEAR  = 'R'; // Clear signal(s)
constexpr char MSG_SIGNAL_SET    = 'S'; // Set signal(s)
constexpr char MSG_DATA_BYTE     = 'D'; // Cmd/data byte (no EOI)
constexpr char MSG_END_BYTE      = 'E'; // Data byte (with EOI)
constexpr char MSG_PP_DATA       = 'P'; // Parallel poll data
constexpr char MSG_PP_REQUEST    = 'Q'; // Request PP data
constexpr char MSG_ECHO_REQ      = 'J'; // Heartbeat msg: echo request
constexpr char MSG_ECHO_REPLY    = 'K'; // Heartbeat msg: echo reply

// Size of sectors
constexpr unsigned SECTOR_SIZE  = 256;

// ********************************************************************************
// Remote488MsgIO
//
struct ConnectionClosed
{
};

class Remote488MsgIO
{
public:
	Remote488MsgIO(int socket);

	struct Msg {
		char msg_type;
		uint8_t msg_data;
	};

	bool has_msg() const;
	void get_msg(Msg& msg);

	void send_msg(const Msg& msg);
	void send_data(const std::vector<uint8_t>& data , bool eoi_at_end = false);
	void send_end_byte(uint8_t byte);
	void send_pp_state(uint8_t pp_state);

private:
	int socket_fd;
	std::recursive_mutex msg_lock;
	mutable std::mutex cv_lock;
	std::condition_variable cv;

	typedef std::queue<Msg> queue_t;
	queue_t q;

	std::thread th;

	enum class RxFSMState {
		REM_RX_WAIT_CH,
		REM_RX_WAIT_COLON,
		REM_RX_WAIT_1ST_HEX,
		REM_RX_WAIT_2ND_HEX,
		REM_RX_WAIT_SEP,
		REM_RX_WAIT_WS
	};

	static constexpr char CONNECTION_END = 0xff;

	static std::string format_msg(const Msg &msg);
	void sendall(const std::string& str);
	void my_thread();
	static bool a2hex(char c , uint8_t& out);
	static bool is_msg_type(char c);
	static bool is_terminator(char c);
	static bool is_space(char c);
};

Remote488MsgIO::Remote488MsgIO(int socket)
	: socket_fd(socket),
	  msg_lock(),
	  cv_lock(),
	  cv(),
	  q(),
	  th()
{
	th = std::thread([this]{ my_thread(); });
	th.detach();
}

bool Remote488MsgIO::has_msg() const
{
	return !q.empty();
}

void Remote488MsgIO::get_msg(Msg& msg)
{
	std::unique_lock<std::mutex> lk{cv_lock};
	cv.wait(lk , [this]{ return has_msg(); });
	msg = q.front();
	q.pop();
	if (msg.msg_type == CONNECTION_END) {
		throw ConnectionClosed();
	}
}

void Remote488MsgIO::send_msg(const Msg &msg)
{
	std::string msg_str{format_msg(msg)};

	std::lock_guard<std::recursive_mutex> lock{msg_lock};

	sendall(msg_str);
}

void Remote488MsgIO::send_data(const std::vector<uint8_t>& data , bool eoi_at_end)
{
	std::string str{};

	for (auto i = data.cbegin(); i != data.cend(); i++) {
		Msg msg{ MSG_DATA_BYTE , *i };
		if (eoi_at_end && (i + 1) == data.cend()) {
			msg.msg_type = MSG_END_BYTE;
		}
		str.append(format_msg(msg));
	}

	if (str.size() > 0) {
		std::lock_guard<std::recursive_mutex> lock{msg_lock};

		sendall(str);
	}
}

void Remote488MsgIO::send_end_byte(uint8_t byte)
{
	Msg msg{ MSG_END_BYTE , byte };
	send_msg(msg);
}

void Remote488MsgIO::send_pp_state(uint8_t pp_state)
{
	Msg msg{ MSG_PP_DATA , pp_state };
	send_msg(msg);
}

std::string Remote488MsgIO::format_msg(const Msg &msg)
{
	char msg_str[ 8 ];
	sprintf(msg_str , "%c:%02x\n" , msg.msg_type , msg.msg_data);
	return std::string{msg_str};
}

void Remote488MsgIO::sendall(const std::string& str)
{
	size_t to_send = str.size();
	const char *buffer = str.c_str();
	int res;

	while (to_send > 0) {
		while ((res = write(socket_fd , buffer , to_send)) < 0) {
			if (errno != EINTR) {
				return;
			}
		}
		to_send -= res;
		buffer += res;
	}
}

void Remote488MsgIO::my_thread()
{
	RxFSMState state = RxFSMState::REM_RX_WAIT_CH;
	Msg msg{ 0 , 0 };

	try {
		while (true) {
			char buffer[ 256 ];
			ssize_t res;
			while ((res = read(socket_fd , buffer , sizeof(buffer))) <= 0) {
				if (res == 0 || errno != EINTR) {
					throw ConnectionClosed{};
				}
			}
			const char *p = buffer;
			while (res > 0) {
				char c = *p++;
				res--;
				switch (state) {
				case RxFSMState::REM_RX_WAIT_CH:
					if (is_msg_type(c)) {
						msg.msg_type = c;
						state = RxFSMState::REM_RX_WAIT_COLON;
					} else if (!is_space(c)) {
						state = RxFSMState::REM_RX_WAIT_WS;
					}
					break;

				case RxFSMState::REM_RX_WAIT_COLON:
					if (c == ':') {
						state = RxFSMState::REM_RX_WAIT_1ST_HEX;
					} else {
						state = RxFSMState::REM_RX_WAIT_WS;
					}
					break;

				case RxFSMState::REM_RX_WAIT_1ST_HEX:
					if (a2hex(c , msg.msg_data)) {
						state = RxFSMState::REM_RX_WAIT_2ND_HEX;
					} else {
						state = RxFSMState::REM_RX_WAIT_WS;
					}
					break;

				case RxFSMState::REM_RX_WAIT_2ND_HEX:
					{
						uint8_t tmp;
						if (a2hex(c , tmp)) {
							msg.msg_data = (msg.msg_data << 4) | tmp;
							state = RxFSMState::REM_RX_WAIT_SEP;
						} else {
							state = RxFSMState::REM_RX_WAIT_WS;
						}
					}
					break;

				case RxFSMState::REM_RX_WAIT_SEP:
					if (is_terminator(c) || is_space(c)) {
						state = RxFSMState::REM_RX_WAIT_CH;
						if (msg.msg_type == MSG_ECHO_REQ) {
							Msg msg_reply{ MSG_ECHO_REPLY , 0 };
							send_msg(msg_reply);
						} else {
							std::lock_guard<std::mutex> lk{cv_lock};
							q.push(msg);
							cv.notify_one();
						}
					} else {
						state = RxFSMState::REM_RX_WAIT_WS;
					}
					break;

				case RxFSMState::REM_RX_WAIT_WS:
					if (is_terminator(c) || is_space(c)) {
						state = RxFSMState::REM_RX_WAIT_CH;
					}
					break;
				}
			}
		}
	}
	catch (ConnectionClosed) {
		// Connection closed
		Msg msg{ CONNECTION_END , 0 };
		std::lock_guard<std::mutex> lk{cv_lock};
		q.push(msg);
		cv.notify_one();
	}
}

bool Remote488MsgIO::a2hex(char c , uint8_t& out)
{
	if (c >= '0' && c <= '9') {
		out = c - '0';
		return true;
	} else if (c >= 'a' && c <= 'f') {
		out = c - 'a' + 10;
		return true;
	} else if (c >= 'A' && c <= 'F') {
		out = c - 'A' + 10;
		return true;
	} else {
		return false;
	}
}

bool Remote488MsgIO::is_msg_type(char c)
{
	// Recognize type of input messages
	return c == MSG_SIGNAL_CLEAR ||
		c == MSG_SIGNAL_SET ||
		c == MSG_DATA_BYTE ||
		c == MSG_END_BYTE ||
		c == MSG_PP_REQUEST ||
		c == MSG_ECHO_REQ;
}

bool Remote488MsgIO::is_terminator(char c)
{
	// Match message terminator characters
	return c == ',' ||
		c == ';';
}

bool Remote488MsgIO::is_space(char c)
{
	// Match whitespace characters
	return c == ' ' ||
		c == '\t' ||
		c == '\r' ||
		c == '\n';
}

// ********************************************************************************
// CHS tuple
//

// Type of LBA
typedef unsigned lba_t;

class CHSOutOfRange
{
};

class LBAOutOfRange
{
};

class CHS
{
public:
	CHS(const uint8_t* byte_repr);
	CHS(unsigned c , unsigned h , unsigned s);
	CHS();

	std::string to_string() const;

	void to_byte_repr(uint8_t *out) const;

	lba_t to_lba(const CHS& geometry) const;
	static CHS from_lba(lba_t lba , const CHS& geometry);

	lba_t get_max_lba() const;
private:
	unsigned my_c , my_h , my_s;

	void check_range() const;
};

CHS::CHS(const uint8_t* byte_repr)
{
	my_c = (unsigned(byte_repr[ 0 ]) << 8) | byte_repr[ 1 ];
	my_h = byte_repr[ 2 ];
	my_s = byte_repr[ 3 ];
}

CHS::CHS(unsigned c , unsigned h , unsigned s)
	: my_c(c),
	  my_h(h),
	  my_s(s)
{
}

CHS::CHS()
	: my_c(0),
	  my_h(0),
	  my_s(0)
{
}

std::string CHS::to_string() const
{
	char s[ 16 ];
	sprintf(s, "(%u:%u:%u)" , my_c , my_h , my_s);
	return std::string{s};
}

void CHS::to_byte_repr(uint8_t *out) const
{
	check_range();
	*out++ = uint8_t(my_c >> 8);
	*out++ = uint8_t(my_c & 0xff);
	*out++ = uint8_t(my_h);
	*out = uint8_t(my_s);
}

lba_t CHS::to_lba(const CHS& geometry) const
{
	check_range();
	if (my_c >= geometry.my_c ||
		my_h >= geometry.my_h ||
		my_s >= geometry.my_s) {
		throw CHSOutOfRange();
	}
	return (my_c * geometry.my_h + my_h) * geometry.my_s + my_s;
}

CHS CHS::from_lba(lba_t lba , const CHS& geometry)
{
	if (lba > geometry.get_max_lba()) {
		throw LBAOutOfRange();
	}
	unsigned tmp = lba / geometry.my_s;
	unsigned my_s = lba - tmp * geometry.my_s;
	unsigned my_c = tmp / geometry.my_h;
	unsigned my_h = tmp - my_c * geometry.my_h;
	return CHS(my_c , my_h , my_s);
}

lba_t CHS::get_max_lba() const
{
	return my_c * my_h * my_s;
}

void CHS::check_range() const
{
	if (my_c >= 0x10000U || my_h >= 0x100U || my_s >= 0x100U) {
		throw CHSOutOfRange();
	}
}

// ********************************************************************************
// IEEE-488 Commands
//

// Forward declarations
class DecodedCmd;

typedef std::unique_ptr<DecodedCmd> dec_cmd_ptr;

// ////////////////////////////////////////////////////////////////////////////////
// Generic (raw/undecoded) bus command
class BusCmd
{
public:
	virtual ~BusCmd();

	virtual std::string to_string() const = 0;
	virtual dec_cmd_ptr decode() = 0;
};

BusCmd::~BusCmd()
{
}

typedef std::unique_ptr<BusCmd> raw_cmd_ptr;

// ////////////////////////////////////////////////////////////////////////////////
// IDENTIFY command
class IdentifyCmd : public BusCmd
{
public:
	IdentifyCmd();

	virtual std::string to_string() const override;
	virtual dec_cmd_ptr decode() override;
};

IdentifyCmd::IdentifyCmd()
{
}

std::string IdentifyCmd::to_string() const
{
	return "IDENTIFY";
}

// ////////////////////////////////////////////////////////////////////////////////
// Parallel poll en/dis command
class ParallelPoll : public BusCmd
{
public:
	ParallelPoll(bool state);

	virtual std::string to_string() const override;
	virtual dec_cmd_ptr decode() override;
private:
	bool enable;
};

ParallelPoll::ParallelPoll(bool state)
	: enable(state)
{
}

std::string ParallelPoll::to_string() const
{
	char str[ 16 ];
	sprintf(str, "PP %d" , enable);
	return std::string(str);
}

// ////////////////////////////////////////////////////////////////////////////////
// Device clear command
class DeviceClear : public BusCmd
{
public:
	DeviceClear();

	virtual std::string to_string() const override;
	virtual dec_cmd_ptr decode() override;
};

DeviceClear::DeviceClear()
{
}

std::string DeviceClear::to_string() const
{
	return "CLEAR";
}

// ////////////////////////////////////////////////////////////////////////////////
// Commands that are differentiated by a secondary address (either Talk or Listen commands)
class SecAddrCmd : public BusCmd
{
public:
	SecAddrCmd(uint8_t sa);

	uint8_t get_sa() const { return sec_addr; }
protected:
	uint8_t sec_addr;
};

SecAddrCmd::SecAddrCmd(uint8_t sa)
	: sec_addr(sa)
{
}

// ////////////////////////////////////////////////////////////////////////////////
// Listen commands
class ListenCmd : public SecAddrCmd
{
public:
	ListenCmd(uint8_t sa);

	virtual std::string to_string() const override;
	virtual dec_cmd_ptr decode() override;

	void add_parameter(uint8_t p);
protected:
	std::vector<uint8_t> params;
};

ListenCmd::ListenCmd(uint8_t sa)
	: SecAddrCmd(sa)
{
}

std::string ListenCmd::to_string() const
{
	char str[ 16 ];
	sprintf(str, "LISTEN %02x:" , sec_addr);
	std::string out{str};
	for (auto b : params) {
		sprintf(str, "%02x " , b);
		out += str;
	}
	return out;
}

void ListenCmd::add_parameter(uint8_t p)
{
	params.push_back(p);
}

// ////////////////////////////////////////////////////////////////////////////////
// Talk commands
class TalkCmd : public SecAddrCmd
{
public:
	TalkCmd(uint8_t sa);

	virtual std::string to_string() const override;
	virtual dec_cmd_ptr decode() override;
};

TalkCmd::TalkCmd(uint8_t sa)
	: SecAddrCmd(sa)
{
}

std::string TalkCmd::to_string() const
{
	char str[ 16 ];
	sprintf(str, "TALK %02x:" , sec_addr);
	return str;
}

// ////////////////////////////////////////////////////////////////////////////////
// Decoded commands

// Forward declarations
class DriveState;

class DecodedCmd
{
public:
	virtual ~DecodedCmd();

	virtual std::string to_string() const = 0;
	virtual void exec(DriveState& state) = 0;
	virtual bool pp_enable() const = 0;
};

DecodedCmd::~DecodedCmd()
{
}

// ////////////////////////////////////////////////////////////////////////////////
// IDENTIFY decoded command
class DecIdentifyCmd : public DecodedCmd
{
public:
	DecIdentifyCmd();

	virtual std::string to_string() const override;
	virtual void exec(DriveState& state) override;
	virtual bool pp_enable() const override;
};

// Decoder from IdentifyCmd to DecIdentifyCmd
dec_cmd_ptr IdentifyCmd::decode()
{
	return std::make_unique<DecIdentifyCmd>();
}

DecIdentifyCmd::DecIdentifyCmd()
{
}

std::string DecIdentifyCmd::to_string() const
{
	return "IDENTIFY";
}

bool DecIdentifyCmd::pp_enable() const
{
	return false;
}

// ////////////////////////////////////////////////////////////////////////////////
// Parallel poll en/dis decoded command
class DecParallelPoll : public DecodedCmd
{
public:
	DecParallelPoll(bool state);

	virtual std::string to_string() const override;
	virtual void exec(DriveState& state) override;
	virtual bool pp_enable() const override;
private:
	bool enable;
};

// Decoder from ParallelPoll to DecParallelPoll
dec_cmd_ptr ParallelPoll::decode()
{
	return std::make_unique<DecParallelPoll>(enable);
}

DecParallelPoll::DecParallelPoll(bool state)
	: enable(state)
{
}

std::string DecParallelPoll::to_string() const
{
	char str[ 16 ];
	sprintf(str, "PP %d" , enable);
	return std::string(str);
}

bool DecParallelPoll::pp_enable() const
{
	return false;
}

// ////////////////////////////////////////////////////////////////////////////////
// Device clear decoded command
class DecDeviceClear : public DecodedCmd
{
public:
	DecDeviceClear();

	virtual std::string to_string() const override;
	virtual void exec(DriveState& state) override;
	virtual bool pp_enable() const override;
private:
	bool enable;
};

// Decoder from DeviceClear to DecDeviceClear
dec_cmd_ptr DeviceClear::decode()
{
	return std::make_unique<DecDeviceClear>();
}

DecDeviceClear::DecDeviceClear()
{
}

std::string DecDeviceClear::to_string() const
{
	return "CLEAR";
}

bool DecDeviceClear::pp_enable() const
{
	return true;
}

// ////////////////////////////////////////////////////////////////////////////////
// Unknown talk command
class UnkTalkCmd : public DecodedCmd
{
public:
	UnkTalkCmd(uint8_t sa);

	virtual std::string to_string() const override;
	virtual void exec(DriveState& state) override;
	virtual bool pp_enable() const override;

private:
	uint8_t sec_addr;
};

UnkTalkCmd::UnkTalkCmd(uint8_t sa)
	: sec_addr(sa)
{
}

std::string UnkTalkCmd::to_string() const
{
	char s[ 32 ];
	sprintf(s , "UNKNOWN TALK %02x" , sec_addr);
	return std::string{s};
}

bool UnkTalkCmd::pp_enable() const
{
	return true;
}

// ////////////////////////////////////////////////////////////////////////////////
// Send data command
class TalkSendData : public DecodedCmd
{
public:
	TalkSendData();

	virtual std::string to_string() const override;
	virtual void exec(DriveState& state) override;
	virtual bool pp_enable() const override;
};

TalkSendData::TalkSendData()
{
}

std::string TalkSendData::to_string() const
{
	return "SEND DATA";
}

bool TalkSendData::pp_enable() const
{
	return true;
}

// ////////////////////////////////////////////////////////////////////////////////
// Send address or status command
class TalkSendStatus : public DecodedCmd
{
public:
	TalkSendStatus();

	virtual std::string to_string() const override;
	virtual void exec(DriveState& state) override;
	virtual bool pp_enable() const override;
};

TalkSendStatus::TalkSendStatus()
{
}

std::string TalkSendStatus::to_string() const
{
	return "SEND ADDR/STATUS";
}

bool TalkSendStatus::pp_enable() const
{
	return true;
}

// ////////////////////////////////////////////////////////////////////////////////
// DSJ command
class TalkDSJ : public DecodedCmd
{
public:
	TalkDSJ();

	virtual std::string to_string() const override;
	virtual void exec(DriveState& state) override;
	virtual bool pp_enable() const override;
};

TalkDSJ::TalkDSJ()
{
}

std::string TalkDSJ::to_string() const
{
	return "DSJ";
}

bool TalkDSJ::pp_enable() const
{
	return true;
}

// ////////////////////////////////////////////////////////////////////////////////
// Decoder of talk commands
dec_cmd_ptr TalkCmd::decode()
{
	switch (sec_addr) {
	case 0:
		// Send Data
		return std::make_unique<TalkSendData>();

	case 8:
		// Send address or status
		return std::make_unique<TalkSendStatus>();

	case 0x10:
		// DSJ
		return std::make_unique<TalkDSJ>();

	default:
		return std::make_unique<UnkTalkCmd>(sec_addr);
	}
}

// ////////////////////////////////////////////////////////////////////////////////
// Unknown listen command
class UnkListenCmd : public DecodedCmd
{
public:
	UnkListenCmd(ListenCmd&& cmd);

	virtual std::string to_string() const override;
	virtual void exec(DriveState& state) override;
	virtual bool pp_enable() const override;
private:
	ListenCmd listen_cmd;
};

UnkListenCmd::UnkListenCmd(ListenCmd&& cmd)
	: listen_cmd(std::move(cmd))
{
}

std::string UnkListenCmd::to_string() const
{
	std::string tmp{"UNKNOWN "};
	return tmp + listen_cmd.to_string();
}

bool UnkListenCmd::pp_enable() const
{
	return true;
}

// ////////////////////////////////////////////////////////////////////////////////
// Receive data command
class ListenReceiveData : public DecodedCmd
{
public:
	ListenReceiveData(std::vector<uint8_t>&& params);

	virtual std::string to_string() const override;
	virtual void exec(DriveState& state) override;
	virtual bool pp_enable() const override;
private:
	std::vector<uint8_t> data;
};

ListenReceiveData::ListenReceiveData(std::vector<uint8_t>&& params)
	: data(std::move(params))
{
}

std::string ListenReceiveData::to_string() const
{
	std::string out{"RECEIVE DATA:"};
	for (auto b : data) {
		char str[ 8 ];
		sprintf(str, "%02x " , b);
		out += str;
	}
	return out;
}

bool ListenReceiveData::pp_enable() const
{
	return true;
}

// ////////////////////////////////////////////////////////////////////////////////
// Seek command
class ListenSeek : public DecodedCmd
{
public:
	ListenSeek(unsigned unit , const CHS& chs);

	virtual std::string to_string() const override;
	virtual void exec(DriveState& state) override;
	virtual bool pp_enable() const override;
private:
	unsigned my_unit;
	CHS my_chs;
};

ListenSeek::ListenSeek(unsigned unit , const CHS& chs)
	: my_chs(chs),
	  my_unit(unit)
{
}

std::string ListenSeek::to_string() const
{
	char s[ 16 ];
	sprintf(s, "SEEK %u:" , my_unit);
	return std::string{s} + my_chs.to_string();
}

bool ListenSeek::pp_enable() const
{
	return true;
}

// ////////////////////////////////////////////////////////////////////////////////
// Request status command
class ListenReqStatus : public DecodedCmd
{
public:
	ListenReqStatus(unsigned unit);

	virtual std::string to_string() const override;
	virtual void exec(DriveState& state) override;
	virtual bool pp_enable() const override;
private:
	unsigned my_unit;
};

ListenReqStatus::ListenReqStatus(unsigned unit)
	: my_unit(unit)
{
}

std::string ListenReqStatus::to_string() const
{
	char s[ 16 ];
	sprintf(s, "REQ STATUS %u" , my_unit);
	return std::string{s};
}

bool ListenReqStatus::pp_enable() const
{
	return true;
}

// ////////////////////////////////////////////////////////////////////////////////
// Verify command
class ListenVerify : public DecodedCmd
{
public:
	ListenVerify(unsigned unit , const uint8_t *sec_count);

	virtual std::string to_string() const override;
	virtual void exec(DriveState& state) override;
	virtual bool pp_enable() const override;
private:
	unsigned my_unit;
	unsigned my_sec_count;
};

ListenVerify::ListenVerify(unsigned unit , const uint8_t *sec_count)
	: my_unit(unit)
{
	my_sec_count = (unsigned(sec_count[ 0 ]) << 8) | sec_count[ 1 ];
}

std::string ListenVerify::to_string() const
{
	char s[ 16 ];
	sprintf(s, "VERIFY %u:%u" , my_unit , my_sec_count);
	return std::string{s};
}

bool ListenVerify::pp_enable() const
{
	return true;
}

// ////////////////////////////////////////////////////////////////////////////////
// Request logical address command
class ListenReqLogAddr : public DecodedCmd
{
public:
	ListenReqLogAddr();

	virtual std::string to_string() const override;
	virtual void exec(DriveState& state) override;
	virtual bool pp_enable() const override;
private:
	unsigned my_unit;
	unsigned my_sec_count;
};

ListenReqLogAddr::ListenReqLogAddr()
{
}

std::string ListenReqLogAddr::to_string() const
{
	return std::string{"REQ LOG ADDRESS"};
}

bool ListenReqLogAddr::pp_enable() const
{
	return true;
}

// ////////////////////////////////////////////////////////////////////////////////
// End command
class ListenEnd : public DecodedCmd
{
public:
	ListenEnd();

	virtual std::string to_string() const override;
	virtual void exec(DriveState& state) override;
	virtual bool pp_enable() const override;
private:
	unsigned my_unit;
	unsigned my_sec_count;
};

ListenEnd::ListenEnd()
{
}

std::string ListenEnd::to_string() const
{
	return std::string{"END"};
}

bool ListenEnd::pp_enable() const
{
	return true;
}

// ////////////////////////////////////////////////////////////////////////////////
// Buffered write command
class ListenBuffWr : public DecodedCmd
{
public:
	ListenBuffWr(unsigned unit);

	virtual std::string to_string() const override;
	virtual void exec(DriveState& state) override;
	virtual bool pp_enable() const override;
private:
	unsigned my_unit;
};

ListenBuffWr::ListenBuffWr(unsigned unit)
	: my_unit(unit)
{
}

std::string ListenBuffWr::to_string() const
{
	char s[ 16 ];
	sprintf(s, "BUFFERED WR %u" , my_unit);
	return std::string{s};
}

bool ListenBuffWr::pp_enable() const
{
	return true;
}

// ////////////////////////////////////////////////////////////////////////////////
// Buffered read command
class ListenBuffRd : public DecodedCmd
{
public:
	ListenBuffRd(unsigned unit);

	virtual std::string to_string() const override;
	virtual void exec(DriveState& state) override;
	virtual bool pp_enable() const override;
private:
	unsigned my_unit;
};

ListenBuffRd::ListenBuffRd(unsigned unit)
	: my_unit(unit)
{
}

std::string ListenBuffRd::to_string() const
{
	char s[ 16 ];
	sprintf(s, "BUFFERED RD %u" , my_unit);
	return std::string{s};
}

bool ListenBuffRd::pp_enable() const
{
	return true;
}

// ////////////////////////////////////////////////////////////////////////////////
// Format command
class ListenFormat : public DecodedCmd
{
public:
	ListenFormat(unsigned unit , uint8_t override , uint8_t filler);

	virtual std::string to_string() const override;
	virtual void exec(DriveState& state) override;
	virtual bool pp_enable() const override;
private:
	unsigned my_unit;
	uint8_t my_override;
	uint8_t my_filler;
};

ListenFormat::ListenFormat(unsigned unit , uint8_t override , uint8_t filler)
	: my_unit(unit),
	  my_override(override),
	  my_filler(filler)
{
}

std::string ListenFormat::to_string() const
{
	char s[ 16 ];
	sprintf(s, "FORMAT %u %02x %02x" , my_unit , my_override , my_filler);
	return std::string{s};
}

bool ListenFormat::pp_enable() const
{
	return true;
}

// ////////////////////////////////////////////////////////////////////////////////
// Amigo clear command
class ListenAmigoClear : public DecodedCmd
{
public:
	ListenAmigoClear();

	virtual std::string to_string() const override;
	virtual void exec(DriveState& state) override;
	virtual bool pp_enable() const override;
};

ListenAmigoClear::ListenAmigoClear()
{
}

std::string ListenAmigoClear::to_string() const
{
	return std::string{"AMIGO CLEAR"};
}

bool ListenAmigoClear::pp_enable() const
{
	return false;
}

// ////////////////////////////////////////////////////////////////////////////////
// Decoder of listen commands
dec_cmd_ptr ListenCmd::decode()
{
	switch (sec_addr) {
	case 0:
		// Receive data
		return std::make_unique<ListenReceiveData>(std::move(params));

	case 8:
		if (params.size() == 6 && (params[ 0 ] == 2 || params[ 0 ] == 0x0c)) {
			// Seek & Set address record
			return std::make_unique<ListenSeek>(params[ 1 ] , CHS{ params.data() + 2 });
		} else if (params.size() == 2 && params[ 0 ] == 3) {
			// Request status
			return std::make_unique<ListenReqStatus>(params[ 1 ]);
		} else if (params.size() == 4 && params[ 0 ] == 7) {
			// Verify
			return std::make_unique<ListenVerify>(params[ 1 ] , params.data() + 2);
		} else if (params.size() == 2 && params[ 0 ] == 0x14) {
			// Request logical address
			return std::make_unique<ListenReqLogAddr>();
		} else if (params.size() == 2 && params[ 0 ] == 0x15) {
			// End
			return std::make_unique<ListenEnd>();
		}
		break;

	case 9:
		if (params.size() == 2 && params[ 0 ] == 8) {
			// Buffered write
			return std::make_unique<ListenBuffWr>(params[ 1 ]);
		}
		break;

	case 0x0a:
		if (params.size() == 2 && params[ 0 ] == 3) {
			// Request status
			return std::make_unique<ListenReqStatus>(params[ 1 ]);
		} else if (params.size() == 2 && params[ 0 ] == 5) {
			// Buffered read
			return std::make_unique<ListenBuffRd>(params[ 1 ]);
		} else if (params.size() == 2 && params[ 0 ] == 0x14) {
			// Request logical address
			return std::make_unique<ListenReqLogAddr>();
		}
		break;

	case 0x0b:
		if (params.size() == 2 && params[ 0 ] == 5) {
			// Buffered read/verify
			return std::make_unique<ListenBuffRd>(params[ 1 ]);
		}
		break;

	case 0x0c:
		if (params.size() == 5 && params[ 0 ] == 0x18) {
			// Format
			return std::make_unique<ListenFormat>(params[ 1 ] , params[ 2 ] , params[ 4 ]);
		}
		break;

	case 0x10:
		if (params.size() == 1) {
			// Amigo clear
			return std::make_unique<ListenAmigoClear>();
		}
		break;

	default:
		break;
	}

	return std::make_unique<UnkListenCmd>(std::move(*this));
}

// ********************************************************************************
// Bus command decoder
//
class CmdDecoder
{
public:
	CmdDecoder(Remote488MsgIO& intf , uint8_t hpib_address);

	raw_cmd_ptr get_cmd();

private:
	Remote488MsgIO& io;
	uint8_t my_mta;
	uint8_t my_mla;
	uint8_t my_msa;

	enum class SAFSMState {
		SA_NONE,
		SA_PACS,
		SA_TPAS,
		SA_LPAS,
		SA_UNT
	};

	SAFSMState sa_state;

	enum class DecFSMState {
		DEC_IDLE,
		DEC_MTA_SA,
		DEC_MLA_SA
	};

	DecFSMState dec_state;

	bool talker;
	bool listener;
	bool pp_state;
	uint8_t signals;
	raw_cmd_ptr pending_cmd;
};

CmdDecoder::CmdDecoder(Remote488MsgIO& intf , uint8_t hpib_address)
	: io(intf)
{
	my_mta = (hpib_address & 0x1f) | 0x40;
	my_mla = (hpib_address & 0x1f) | 0x20;
	my_msa = (hpib_address & 0x1f) | 0x60;

	sa_state = SAFSMState::SA_NONE;
	dec_state = DecFSMState::DEC_IDLE;
	talker = false;
	listener = false;
	pp_state = false;
	signals = ~0;
}

raw_cmd_ptr CmdDecoder::get_cmd()
{
	while (true) {
		Remote488MsgIO::Msg msg;
		io.get_msg(msg);
		switch (msg.msg_type) {
		case MSG_SIGNAL_CLEAR:
			signals &= ~msg.msg_data;
			break;

		case MSG_SIGNAL_SET:
			signals |= msg.msg_data;
			break;

		case MSG_PP_REQUEST:
			continue;
		}
		bool is_cmd = (signals & 1) == 0 && msg.msg_type == MSG_DATA_BYTE;
		if (is_cmd) {
			msg.msg_data &= 0x7f;
			bool is_pcg = (msg.msg_data & 0x60) != 0x60;
			if (is_pcg) {
				sa_state = SAFSMState::SA_NONE;
			}
			if (msg.msg_data == 0x05 && listener) {
				// Parallel Poll Configure
				sa_state = SAFSMState::SA_PACS;
			} else if (msg.msg_data == 0x15) {
				// Parallel Poll Unconfigure
				// TODO:
			} else if (listener && msg.msg_data == 0x3f) {
				// UNL
				listener = false;
				dec_state = DecFSMState::DEC_IDLE;
				if (!pp_state) {
					pp_state = true;
					return std::make_unique<ParallelPoll>(true);
				}
			} else if (msg.msg_data == 0x5f) {
				// UNT
				talker = false;
				dec_state = DecFSMState::DEC_IDLE;
				sa_state = SAFSMState::SA_UNT;
				if (!pp_state) {
					pp_state = true;
					return std::make_unique<ParallelPoll>(true);
				}
			} else if (msg.msg_data == my_mla) {
				// MLA
				listener = true;
				dec_state = DecFSMState::DEC_IDLE;
				sa_state = SAFSMState::SA_LPAS;
			} else if (msg.msg_data == my_mta) {
				// MTA
				talker = true;
				dec_state = DecFSMState::DEC_IDLE;
				sa_state = SAFSMState::SA_TPAS;
			} else if (talker && (msg.msg_data & 0x60) == 0x40) {
				// OTA
				talker = false;
				dec_state = DecFSMState::DEC_IDLE;
				if (!pp_state) {
					pp_state = true;
					return std::make_unique<ParallelPoll>(true);
				}
			} else if ((listener && msg.msg_data == 0x04) || msg.msg_data == 0x14) {
				// Device clear
				dec_state = DecFSMState::DEC_IDLE;
				return std::make_unique<DeviceClear>();
			} else if (!is_pcg) {
				switch (sa_state) {
				case SAFSMState::SA_PACS:
					// TODO: PPE/PPD
					break;

				case SAFSMState::SA_TPAS:
					// MTA + SA
					dec_state = DecFSMState::DEC_MTA_SA;
					pending_cmd = std::make_unique<TalkCmd>(msg.msg_data & 0x1f);
					if (pp_state) {
						pp_state = false;
						return std::make_unique<ParallelPoll>(false);
					}
					break;

				case SAFSMState::SA_LPAS:
					// MLA + SA
					dec_state = DecFSMState::DEC_MLA_SA;
					pending_cmd = std::make_unique<ListenCmd>(msg.msg_data & 0x1f);
					if (pp_state) {
						pp_state = false;
						return std::make_unique<ParallelPoll>(false);
					}
					break;

				case SAFSMState::SA_UNT:
					// UNT + SA
					if (msg.msg_data == my_msa) {
						pending_cmd = std::make_unique<IdentifyCmd>();
						dec_state = DecFSMState::DEC_MTA_SA;
					}
					break;
				}
			}
		}
		switch (dec_state) {
		case DecFSMState::DEC_MTA_SA:
			if ((signals & 1) != 0) {
				// ATN de-asserted
				dec_state = DecFSMState::DEC_IDLE;
				return std::move(pending_cmd);
			}
			break;

		case DecFSMState::DEC_MLA_SA:
			if (listener && !is_cmd) {
				if (msg.msg_type == MSG_DATA_BYTE || msg.msg_type == MSG_END_BYTE) {
					dynamic_cast<ListenCmd*>(pending_cmd.get())->add_parameter(msg.msg_data);
				}
				if (msg.msg_type == MSG_END_BYTE) {
					dec_state = DecFSMState::DEC_IDLE;
					return std::move(pending_cmd);
				}
			}
			break;
		}
	}
}

// ********************************************************************************
// Fixed data of drives
//
struct FixedData
{
	std::vector<uint8_t> id;    // Identify sequence (2 bytes)
	CHS geometry;               // Geometry of units
	unsigned units;             // Count of units
	bool ignore_fmt_filler;     // Ignore filler byte in format command
};

// ********************************************************************************
// Unit state
//
class UnitState
{
public:
	UnitState(FILE *image , const FixedData& fd);

	bool is_ready() const;

	lba_t get_lba() const;
	void set_lba(lba_t new_lba);
	bool is_lba_ok() const;

	void format_img(uint8_t filler);
	void write_img(const std::vector<uint8_t>& data);
	std::vector<uint8_t> read_img();

	void to_byte_repr(uint8_t *out) const;

	bool a_bit() const;
	bool c_bit() const;
	bool f_bit() const;
	bool w_bit() const;
	bool& a_bit();
	bool& c_bit();
	bool& f_bit();
	bool& w_bit();
private:
	FILE *my_img;
	const FixedData& fixed_data;
	lba_t current_lba;
	bool my_a_bit;
	bool my_c_bit;
	bool my_f_bit;
	bool my_w_bit;
	uint8_t ss;
	uint8_t tttt;
};

UnitState::UnitState(FILE *image , const FixedData& fd)
	: my_img(image),
	  fixed_data(fd),
	  current_lba(0),
	  my_a_bit(false),
	  my_c_bit(false),
	  my_f_bit(true),
	  my_w_bit(false),
	  ss(0),
	  tttt(6)
{
	if (!is_ready()) {
		// Drive not ready
		ss = 3;
		my_f_bit = false;
	}
}

bool UnitState::is_ready() const
{
	return my_img != NULL;
}

lba_t UnitState::get_lba() const
{
	return current_lba;
}

void UnitState::set_lba(lba_t new_lba)
{
	current_lba = new_lba;
}

bool UnitState::is_lba_ok() const
{
	return current_lba < fixed_data.geometry.get_max_lba();
}

void UnitState::format_img(uint8_t filler)
{
	if (is_ready()) {
		fseek(my_img , 0 , SEEK_SET);
		uint8_t empty[ SECTOR_SIZE ];
		memset(empty, filler, sizeof(empty));
		lba_t max_lba = fixed_data.geometry.get_max_lba();
		for (unsigned i = 0; i < max_lba; i++) {
			fwrite(empty, 1, SECTOR_SIZE, my_img);
		}
		current_lba = 0;
	}
}

void UnitState::write_img(const std::vector<uint8_t>& data)
{
	if (is_ready()) {
		fseek(my_img , current_lba * SECTOR_SIZE , SEEK_SET);
		auto len = data.size();
		if (len > SECTOR_SIZE) {
			len = SECTOR_SIZE;
		}
		fwrite(data.data() , 1 , len , my_img);
		if (len < SECTOR_SIZE) {
			uint8_t fill = 0;
			while (len < SECTOR_SIZE) {
				fwrite(&fill , 1 , 1 , my_img);
				len++;
			}
		}
		current_lba++;
	}
}

std::vector<uint8_t> UnitState::read_img()
{
	std::vector<uint8_t> tmp(SECTOR_SIZE);
	if (is_ready()) {
		fseek(my_img , current_lba * SECTOR_SIZE , SEEK_SET);
		fread(tmp.data() , 1 , SECTOR_SIZE , my_img);
		current_lba++;
	}
	return tmp;
}

void UnitState::to_byte_repr(uint8_t *out) const
{
	out[ 0 ] = tttt << 1;
	if (my_c_bit || ss) {
		out[ 0 ] |= 0x80;
	}
	uint8_t res = ss;
	if (my_a_bit) {
		res |= 0x80;
	}
	if (my_w_bit) {
		res |= 0x40;
	}
	if (my_f_bit) {
		res |= 0x08;
	}
	if (my_c_bit) {
		res |= 0x04;
	}
	out[ 1 ] = res;
}

bool UnitState::a_bit() const
{
	return my_a_bit;
}

bool UnitState::c_bit() const
{
	return my_c_bit;
}

bool UnitState::f_bit() const
{
	return my_f_bit;
}

bool UnitState::w_bit() const
{
	return my_w_bit;
}

bool& UnitState::a_bit()
{
	return my_a_bit;
}

bool& UnitState::c_bit()
{
	return my_c_bit;
}

bool& UnitState::f_bit()
{
	return my_f_bit;
}

bool& UnitState::w_bit()
{
	return my_w_bit;
}

// ********************************************************************************
// Drive state
//
class DriveState
{
public:
	DriveState(Remote488MsgIO& intf , const FixedData& fix_data, FILE *fp[]);

	void exec_cmd(DecodedCmd& cmd);

private:
	Remote488MsgIO& io;
	const FixedData& fixed_data;
	std::vector<std::unique_ptr<UnitState>> units;
	uint8_t dsj;
	uint8_t stat1;
	unsigned current_unit;
	unsigned failed_unit;
	bool pp_enabled;
	bool pp_state;
	std::vector<uint8_t> status;
	std::vector<uint8_t> buffer;

	// Command sequencing state
	enum class CmdSeqState {
		SEQ_IDLE,   // Not waiting for a particular cmd
		SEQ_WAIT_SEND_STATUS,   // Waiting for send addr/status cmd
		SEQ_WAIT_SEND_DATA,     // Waiting for send data cmd
		SEQ_WAIT_RECEIVE_DATA,  // Waiting for receive data cmd
		SEQ_WAIT_CLEAR			// Waiting for clear cmd
	};

	CmdSeqState cmd_seq_state;

	void set_pp(bool state);

	void set_seq_error(bool talker);
	bool require_seq_state(CmdSeqState req_state , bool talker);
	bool is_dsj_ok() const;
	UnitState *select_unit(unsigned unit_no);
	UnitState& get_current_unit();
	bool dsj1_holdoff() const;
	bool is_lba_ok();

	// Error codes
	static constexpr uint8_t ERROR_BAD_CMD  = 0x01; // Unknown command
	static constexpr uint8_t ERROR_IO       = 0x0a; // I/O error
	static constexpr uint8_t ERROR_STAT2    = 0x13; // Some error in stat2
	static constexpr uint8_t ERROR_NO_UNIT  = 0x17; // Unit # out of range
	static constexpr uint8_t ERROR_ATTENTION= 0x1f; // Unit attention

	void set_error(uint8_t error_code);
	void clear_errors();
	void clear_dsj();
	void amigo_clear();

	friend void DecIdentifyCmd::exec(DriveState& state);
	friend void DecParallelPoll::exec(DriveState& state);
	friend void DecDeviceClear::exec(DriveState& state);
	friend void UnkTalkCmd::exec(DriveState& state);
	friend void TalkSendData::exec(DriveState& state);
	friend void TalkSendStatus::exec(DriveState& state);
	friend void TalkDSJ::exec(DriveState& state);
	friend void UnkListenCmd::exec(DriveState& state);
	friend void ListenReceiveData::exec(DriveState& state);
	friend void ListenSeek::exec(DriveState& state);
	friend void ListenReqStatus::exec(DriveState& state);
	friend void ListenVerify::exec(DriveState& state);
	friend void ListenReqLogAddr::exec(DriveState& state);
	friend void ListenEnd::exec(DriveState& state);
	friend void ListenBuffWr::exec(DriveState& state);
	friend void ListenBuffRd::exec(DriveState& state);
	friend void ListenFormat::exec(DriveState& state);
	friend void ListenAmigoClear::exec(DriveState& state);
};

DriveState::DriveState(Remote488MsgIO& intf , const FixedData& fix_data , FILE *fp[])
	: io(intf),
	  fixed_data(fix_data),
	  units(),
	  dsj(2),
	  stat1(0),
	  current_unit(0),
	  failed_unit(0),
	  pp_enabled(true),
	  pp_state(false),
	  status(4),
	  buffer(),
	  cmd_seq_state(CmdSeqState::SEQ_IDLE)
{
	for (unsigned i = 0; i < fixed_data.units; i++) {
		units.push_back(std::make_unique<UnitState>(fp[ i ] , fixed_data));
	}
}

void DriveState::exec_cmd(DecodedCmd& cmd)
{
	bool en_pp = cmd.pp_enable();
	if (en_pp) {
		pp_enabled = true;
	}
	cmd.exec(*this);
	if (en_pp) {
		set_pp(true);
	}
}

void DriveState::set_pp(bool state)
{
	// TODO: better
	bool new_state = pp_enabled && state;
	if (new_state != pp_state) {
		pp_state = new_state;
		io.send_pp_state(pp_state ? 0x80 : 0x00);
	}
}

void DriveState::set_seq_error(bool talker)
{
	cmd_seq_state = CmdSeqState::SEQ_IDLE;
	if (dsj == 0) {
		set_error(ERROR_IO);
	}
	if (talker) {
		io.send_end_byte(1);
	}
}

bool DriveState::require_seq_state(CmdSeqState req_state , bool talker)
{
	if (cmd_seq_state != req_state) {
		set_seq_error(talker);
		cmd_seq_state = CmdSeqState::SEQ_IDLE;
		return false;
	} else {
		return true;
	}
}

bool DriveState::is_dsj_ok() const
{
	return dsj != 2;
}

UnitState *DriveState::select_unit(unsigned unit_no)
{
	UnitState *unit = nullptr;
	if (unit_no < fixed_data.units) {
		current_unit = unit_no;
		unit = units[ current_unit ].get();
		if (unit->f_bit() || !unit->is_ready()) {
			set_error(ERROR_STAT2);
			unit = nullptr;
		}
	} else {
		set_error(ERROR_NO_UNIT);
	}
	return unit;
}

UnitState& DriveState::get_current_unit()
{
	return *units[ current_unit ];
}

bool DriveState::dsj1_holdoff() const
{
	return dsj == 1 && stat1 != ERROR_BAD_CMD && stat1 != ERROR_IO;
}

bool DriveState::is_lba_ok()
{
	UnitState& unit = get_current_unit();
	if (unit.is_lba_ok()) {
		return true;
	} else {
		set_error(ERROR_ATTENTION);
		unit.a_bit() = true;
		unit.c_bit() = true;
		return false;
	}
}

void DriveState::set_error(uint8_t error_code)
{
	stat1 = error_code;
	failed_unit = current_unit;
	if (dsj != 2) {
		dsj = 1;
	}
}

void DriveState::clear_errors()
{
	stat1 = 0;
	dsj = 0;
}

void DriveState::clear_dsj()
{
	if (dsj != 2) {
		dsj = 0;
	}
}

void DriveState::amigo_clear()
{
	for (auto& u : units) {
		u->a_bit() = false;
		u->c_bit() = false;
		u->f_bit() = false;
		u->set_lba(0);
	}
	current_unit = 0;
	cmd_seq_state = CmdSeqState::SEQ_IDLE;
	clear_errors();
}

// ********************************************************************************
// Command execution
//
void DecIdentifyCmd::exec(DriveState& state)
{
	state.io.send_data(state.fixed_data.id , true);
}

void DecParallelPoll::exec(DriveState& state)
{
	state.set_pp(enable);
}

void DecDeviceClear::exec(DriveState& state)
{
	state.amigo_clear();
}

void UnkTalkCmd::exec(DriveState& state)
{
	// TODO:
}

void TalkSendData::exec(DriveState& state)
{
	if (state.require_seq_state(DriveState::CmdSeqState::SEQ_WAIT_SEND_DATA, true)) {
		state.io.send_data(state.buffer);
		state.cmd_seq_state = DriveState::CmdSeqState::SEQ_IDLE;
	}
}

void TalkSendStatus::exec(DriveState& state)
{
	if (state.require_seq_state(DriveState::CmdSeqState::SEQ_WAIT_SEND_STATUS, true)) {
		state.io.send_data(state.status);
		state.cmd_seq_state = DriveState::CmdSeqState::SEQ_IDLE;
	}
}

void TalkDSJ::exec(DriveState& state)
{
	if (state.require_seq_state(DriveState::CmdSeqState::SEQ_IDLE, true)) {
		state.io.send_end_byte(state.dsj);
		if (state.dsj == 2) {
			state.dsj = 0;
		}
	}
	state.pp_enabled = false;
}

void UnkListenCmd::exec(DriveState& state)
{
	state.set_error(DriveState::ERROR_IO);
	state.cmd_seq_state = DriveState::CmdSeqState::SEQ_IDLE;
}

void ListenReceiveData::exec(DriveState& state)
{
	if (state.require_seq_state(DriveState::CmdSeqState::SEQ_WAIT_RECEIVE_DATA, false)) {
		UnitState& unit = state.get_current_unit();
		state.buffer = std::move(data);
		unit.write_img(state.buffer);
		state.clear_errors();
		state.cmd_seq_state = DriveState::CmdSeqState::SEQ_IDLE;
	}
}

void ListenSeek::exec(DriveState& state)
{
	UnitState *unit;
	if (state.require_seq_state(DriveState::CmdSeqState::SEQ_IDLE, false) &&
		state.is_dsj_ok() &&
		(unit = state.select_unit(my_unit)) != nullptr) {
		state.set_error(DriveState::ERROR_ATTENTION);
		unit->a_bit() = true;
		try {
			lba_t new_lba = my_chs.to_lba(state.fixed_data.geometry);
			unit->set_lba(new_lba);
			state.clear_dsj();
		}
		catch (CHSOutOfRange) {
			unit->c_bit() = true;
		}
	}
}

void ListenReqStatus::exec(DriveState& state)
{
	if (state.require_seq_state(DriveState::CmdSeqState::SEQ_IDLE, false) &&
		state.is_dsj_ok()) {
		UnitState *unit = nullptr;
		if (my_unit < state.fixed_data.units) {
			state.current_unit = my_unit;
			unit = &state.get_current_unit();
			state.status[ 0 ] = state.stat1;
			state.status[ 1 ] = uint8_t(state.current_unit);
			unit->to_byte_repr(state.status.data() + 2);
		} else {
			// Invalid unit number
			state.status[ 0 ] = DriveState::ERROR_NO_UNIT;
			state.status[ 1 ] = uint8_t(my_unit);
			state.status[ 2 ] = 0;
			state.status[ 3 ] = 0;
			unit = &state.get_current_unit();
		}
		unit->a_bit() = false;
		unit->f_bit() = false;
		unit->c_bit() = false;
		state.clear_errors();
		state.cmd_seq_state = DriveState::CmdSeqState::SEQ_WAIT_SEND_STATUS;
	}
}

void ListenVerify::exec(DriveState& state)
{
	UnitState *unit;
	if (state.require_seq_state(DriveState::CmdSeqState::SEQ_IDLE, false) &&
		state.is_dsj_ok() &&
		(unit = state.select_unit(my_unit)) != nullptr) {
		if (my_sec_count == 0) {
			// Verify to end of disk
			unit->set_lba(state.fixed_data.geometry.get_max_lba());
		} else {
			lba_t new_lba = std::min(state.fixed_data.geometry.get_max_lba() , unit->get_lba() + my_sec_count);
			unit->set_lba(new_lba);
		}
		state.clear_errors();
	}
}

void ListenReqLogAddr::exec(DriveState& state)
{
	if (state.require_seq_state(DriveState::CmdSeqState::SEQ_IDLE, false) &&
		state.is_dsj_ok()) {
		lba_t current_lba = state.get_current_unit().get_lba();
		CHS current_chs = CHS::from_lba(current_lba, state.fixed_data.geometry);
		current_chs.to_byte_repr(state.status.data());
		state.clear_errors();
		state.cmd_seq_state = DriveState::CmdSeqState::SEQ_WAIT_SEND_STATUS;
	}
}

void ListenEnd::exec(DriveState& state)
{
	if (state.require_seq_state(DriveState::CmdSeqState::SEQ_IDLE, false) &&
		state.is_dsj_ok()) {
		// Not entirely correct
		state.clear_errors();
		state.pp_enabled = false;
	}
}

void ListenBuffWr::exec(DriveState& state)
{
	if (state.require_seq_state(DriveState::CmdSeqState::SEQ_IDLE, false) &&
		state.is_dsj_ok() &&
		state.select_unit(my_unit) != nullptr &&
		!state.dsj1_holdoff() &&
		state.is_lba_ok()) {
		state.cmd_seq_state = DriveState::CmdSeqState::SEQ_WAIT_RECEIVE_DATA;
	}
}

void ListenBuffRd::exec(DriveState& state)
{
	UnitState *unit;
	if (state.require_seq_state(DriveState::CmdSeqState::SEQ_IDLE, false) &&
		state.is_dsj_ok() &&
		(unit = state.select_unit(my_unit)) != nullptr &&
		!state.dsj1_holdoff() &&
		state.is_lba_ok()) {
		state.buffer = unit->read_img();
		state.clear_errors();
		state.cmd_seq_state = DriveState::CmdSeqState::SEQ_WAIT_SEND_DATA;
	}
}

void ListenFormat::exec(DriveState& state)
{
	UnitState *unit;
	if (state.require_seq_state(DriveState::CmdSeqState::SEQ_IDLE, false) &&
		state.is_dsj_ok() &&
		(unit = state.select_unit(my_unit)) != nullptr) {
		if (!state.fixed_data.ignore_fmt_filler || (my_override & 0x80) != 0) {
			unit->format_img(state.fixed_data.ignore_fmt_filler ? 0xff : my_filler);
		}
		unit->set_lba(0);
		state.clear_errors();
	}
}

void ListenAmigoClear::exec(DriveState& state)
{
	if (state.require_seq_state(DriveState::CmdSeqState::SEQ_IDLE, false)) {
		state.cmd_seq_state = DriveState::CmdSeqState::SEQ_WAIT_CLEAR;
	}
}

// ********************************************************************************
static const std::map<std::string , FixedData> models = {
	{ "9134b" , {{ 0x01 , 0x0a } , { 306 , 4 , 31 } , 1 , true}},
	{ "9895"  , {{ 0x00 , 0x81 } , {  77 , 2 , 30 } , 2 , false}}
};

const FixedData *get_fixed_data(const std::string& model)
{
	try {
		const FixedData& fd = models.at(model);
		return &fd;
	}
	catch (std::out_of_range) {
		fprintf(stderr , "Model %s not found\n" , model.c_str());
		fprintf(stderr, "\nAvailable models:\n");
		for (auto& i : models) {
			fprintf(stderr , "%s\n" , i.first.c_str());
		}
		return nullptr;
	}
}

// ********************************************************************************
int main(int argc, char *argv[])
{
	int arg_idx = 1;
	if (arg_idx >= argc) {
		fprintf(stderr , "Missing model name\n");
		return 1;
	}
	const FixedData* fixed_data = get_fixed_data(argv[ arg_idx++ ]);
	if (fixed_data == nullptr) {
		return 1;
	}

	// if ((arg_idx + fixed_data->units) > argc) {
	//  fprintf(stderr , "Missing image file(s) (%u needed)\n" , fixed_data->units);
	//  return 1;
	// }

	FILE *fps[ fixed_data->units ];
	unsigned i;
	for (i = 0; i < fixed_data->units && (arg_idx + i) < argc; i++) {
		printf("Opening image file %s for unit #%u..\n" , argv[ arg_idx + i ] , i);
		if ((fps[ i ] = fopen(argv[ arg_idx + i ] , "r+b")) == NULL) {
			fprintf(stderr, "Can't open %s\n" , argv[ i + 1 ]);
			return 1;
		}
	}
	for (; i < fixed_data->units; i++) {
		printf("No image for unit #%u\n" , i);
		fps[ i ] = NULL;
	}

	int fd;
	if ((fd = socket(AF_INET , SOCK_STREAM , 0)) < 0) {
		perror("socket");
		return 1;
	}
	struct sockaddr_in name;

	name.sin_family = AF_INET;
	name.sin_port = htons(1234);
	name.sin_addr.s_addr = htonl (INADDR_ANY);
	if (bind(fd, (struct sockaddr *)&name, sizeof(name)) < 0) {
		perror("bind");
		return 1;
	}

	puts("Listening...");

	if (listen(fd , 1) < 0) {
		perror("listen");
		return 1;
	}

	int connected_fd;
	socklen_t addr_size = sizeof(name);

	if ((connected_fd = accept(fd , (struct sockaddr *)&name, &addr_size)) < 0) {
		perror("accept");
		return 1;
	}
	close(fd);
	printf("Connected from port %u\n" , ntohs(name.sin_port));

	{
		int option = 1;
		socklen_t option_size = sizeof(option);
		if (setsockopt(connected_fd , IPPROTO_TCP , TCP_NODELAY , &option , option_size) < 0) {
			perror("setsockopt");
			return 1;
		}
	}
	Remote488MsgIO msg_io{connected_fd};
	DriveState ds{ msg_io , *fixed_data , fps };
	CmdDecoder decoder{msg_io, 0};
	try {
		while (true) {
			raw_cmd_ptr cmd = decoder.get_cmd();
			dec_cmd_ptr dec_cmd = cmd->decode();
			puts(dec_cmd->to_string().c_str());
			ds.exec_cmd(*dec_cmd);
		}
	}
	catch (ConnectionClosed) {
		puts("Disconnected!");
	}
	close(connected_fd);
	fcloseall();

	return 0;
}
