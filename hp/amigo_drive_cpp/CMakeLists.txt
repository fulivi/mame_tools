PROJECT(amigo_drive)

SET(CMAKE_CXX_STANDARD 14)
set(CMAKE_CXX_FLAGS_DEBUG -g -O0)
set(CMAKE_CXX_FLAGS_RELEASE -O3)
add_executable(amigo_drive amigo_drive.cpp)
target_link_libraries(amigo_drive pthread)
install(TARGETS amigo_drive DESTINATION ${CMAKE_SOURCE_DIR}/..)
