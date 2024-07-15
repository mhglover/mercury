function quickrate(track_id, html_id, change) {
    // Prevent the default link action here if needed, but since href="#", it might not be necessary.

    console.log('quickrate track:', track_id, html_id, change);
    // Assuming `socket` is your WebSocket connection
    socket.send(JSON.stringify({
                                 'quickrate': {
                                        'track_id': track_id,
                                        'html_id': html_id,
                                        'change': change
                                 }
                }));
}

// Create a new WebSocket.
var socket = new WebSocket(`ws://${window.location.host}/ws`);

document.addEventListener('DOMContentLoaded', (event) => {

	// Connection opened
	socket.addEventListener('open', function (event) {
		console.log('WebSocket connected');
	});

    // Listen for messages
    socket.addEventListener('message', function (event) {
        var message = JSON.parse(event.data); // Parse the entire message

        //hide a block
        // {"hide": "block-id"}
        if(message.hide) {
            console.log('hide:', message.hide);
            var element = document.getElementById(message.hide);
            if (element) {
                element.style.display = 'none';
            }
        }

        //unhide a block
        // {"show": "block-id"}
        if (message.show) {
            console.log('show:', message.show);
            var element = document.getElementById(message.id);
            if (element) {
                element.style.display = 'block';
            }
        }

        // update a block
        // {"update": [{"id": "block-id", "href/class/value": "new-value"}]}
        if (message.update) {
            console.log('update:', message.update);
            var dataList = message.update; // Access the 'update' key for the list

            dataList.forEach(data => {
                var element = document.getElementById(data.id);
                if (element) {
                    Object.keys(data).forEach(key => {
                        // Skip 'id' since it's used to select the element
                        if (key !== 'id') { 
                            if (key === 'value') {
                                // For 'value', update the element's value or innerText
                                if (element.value !== undefined) {
                                    element.value = data[key];
                                } else {
                                    element.innerText = data[key];
                                }
                            } else {
                                // For 'href' and 'class', update the element's attribute
                                element.setAttribute(key, data[key]);
                            }
                        }
                    });
                }
            });
        }
    });
});

