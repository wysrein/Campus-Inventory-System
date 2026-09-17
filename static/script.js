// =====================================
// Campus Hardware Inventory
// Main JavaScript
// =====================================


// =====================================
// Confirm actions before submitting forms
// =====================================

document.addEventListener("DOMContentLoaded", function () {

    const forms = document.querySelectorAll("form");

    forms.forEach(function (form) {

        const action = form.getAttribute("action") || "";

        if (
            action.includes("approve") ||
            action.includes("reject")
        ) {
            form.addEventListener("submit", function (event) {

                const confirmed = confirm(
                    "Are you sure you want to continue with this action?"
                );

                if (!confirmed) {
                    event.preventDefault();
                }
            });
        }
    });

});


// =====================================
// Quantity Validation
// =====================================

function validateQuantity(input) {

    const value = Number(input.value);
    const minimum = Number(input.min);
    const maximum = Number(input.max);

    if (!Number.isInteger(value)) {

        input.setCustomValidity(
            "Quantity must be a whole number."
        );

        return false;
    }

    if (value < minimum) {

        input.setCustomValidity(
            "Quantity must be at least " + minimum + "."
        );

        return false;
    }

    if (value > maximum) {

        input.setCustomValidity(
            "Quantity cannot exceed available stock."
        );

        return false;
    }

    input.setCustomValidity("");

    return true;
}


// =====================================
// Apply quantity validation to number inputs
// =====================================

document.addEventListener("DOMContentLoaded", function () {

    const quantityInputs = document.querySelectorAll(
        'input[type="number"][max]'
    );

    quantityInputs.forEach(function (input) {

        input.addEventListener("input", function () {
            validateQuantity(input);
        });

        input.addEventListener("change", function () {
            validateQuantity(input);
        });

    });

});


// =====================================
// Password Visibility
// =====================================

function togglePassword(fieldId) {

    const field = document.getElementById(fieldId);

    if (!field) {
        return;
    }

    if (field.type === "password") {
        field.type = "text";
    } else {
        field.type = "password";
    }

}


function toggleRegisterPasswords() {

    const password =
        document.getElementById("password");

    const confirmPassword =
        document.getElementById("confirm_password");

    if (!password || !confirmPassword) {
        return;
    }

    if (password.type === "password") {

        password.type = "text";
        confirmPassword.type = "text";

    } else {

        password.type = "password";
        confirmPassword.type = "password";

    }

}


// =====================================
// EQUIPMENT INVENTORY
// =====================================

document.addEventListener("DOMContentLoaded", function () {

    const selectors =
        document.querySelectorAll(".equipment-selector");

    const selectedItemDisplay =
        document.getElementById("selected-item-display");

    const selectedItemId =
        document.getElementById("selected-item-id");

    const borrowQuantity =
        document.getElementById("borrow-quantity");

    const selectedQuantity =
        document.getElementById("selected-quantity");

    const borrowButton =
        document.getElementById("borrow-button");

    const holdButton =
        document.getElementById("hold-button");

    const holdItemId =
        document.getElementById("hold-item-id");

    const holdQuantity =
        document.getElementById("hold-quantity");


    // ---------------------------------
    // Stop if this is not the catalog page
    // ---------------------------------

    if (
        selectors.length === 0 ||
        !selectedItemDisplay ||
        !selectedItemId ||
        !borrowQuantity ||
        !selectedQuantity ||
        !borrowButton ||
        !holdButton ||
        !holdItemId ||
        !holdQuantity
    ) {
        return;
    }


    // ---------------------------------
    // Select Equipment
    // ---------------------------------

    selectors.forEach(function (selector) {

        selector.addEventListener("change", function () {

            const itemId =
                this.dataset.itemId;

            const itemName =
                this.dataset.itemName;

            const availableQuantity =
                parseInt(this.dataset.quantity);


            // Display selected item

            selectedItemDisplay.textContent =
                itemName;


            // Store selected item for borrow

            selectedItemId.value =
                itemId;


            // Store selected item for hold

            holdItemId.value =
                itemId;


            // Set maximum quantity

            borrowQuantity.max =
                availableQuantity;


            // Reset quantity if necessary

            if (
                parseInt(borrowQuantity.value) >
                availableQuantity
            ) {

                borrowQuantity.value = 1;

            }


            // Update borrow quantity

            selectedQuantity.value =
                borrowQuantity.value;


            // Update hold quantity

            holdQuantity.value =
                borrowQuantity.value;


            // Enable / disable buttons

            if (availableQuantity > 0) {

                borrowButton.disabled = false;
                holdButton.disabled = false;

            } else {

                borrowButton.disabled = true;
                holdButton.disabled = true;

            }

        });

    });


    // ---------------------------------
    // Update Quantity
    // ---------------------------------

    borrowQuantity.addEventListener("input", function () {

        let quantity =
            parseInt(this.value);


        if (
            isNaN(quantity) ||
            quantity < 1
        ) {

            quantity = 1;
            this.value = 1;

        }


        const selected =
            document.querySelector(
                ".equipment-selector:checked"
            );


        if (selected) {

            const maxQuantity =
                parseInt(
                    selected.dataset.quantity
                );


            if (quantity > maxQuantity) {

                quantity = maxQuantity;

                this.value =
                    maxQuantity;

            }

        }


        // Borrow quantity

        selectedQuantity.value =
            quantity;


        // Hold quantity

        holdQuantity.value =
            quantity;

    });


    // ---------------------------------
    // Borrow Form Validation
    // ---------------------------------

    const borrowForm =
        document.getElementById("borrow-form");

    if (borrowForm) {

        borrowForm.addEventListener(
            "submit",
            function (event) {

                if (!selectedItemId.value) {

                    event.preventDefault();

                    alert(
                        "Please select an equipment item first."
                    );

                    return;

                }


                if (
                    !borrowQuantity.value ||
                    parseInt(borrowQuantity.value) < 1
                ) {

                    event.preventDefault();

                    alert(
                        "Please enter a valid quantity."
                    );

                    return;

                }


                const selected =
                    document.querySelector(
                        ".equipment-selector:checked"
                    );


                if (selected) {

                    const maxQuantity =
                        parseInt(
                            selected.dataset.quantity
                        );

                    const requestedQuantity =
                        parseInt(
                            borrowQuantity.value
                        );


                    if (
                        requestedQuantity >
                        maxQuantity
                    ) {

                        event.preventDefault();

                        alert(
                            "Requested quantity exceeds available stock."
                        );

                        return;

                    }

                }


                selectedQuantity.value =
                    borrowQuantity.value;

            }
        );

    }


    // ---------------------------------
    // Place Item Hold
    // ---------------------------------

    const holdForm =
        document.getElementById("hold-form");

    if (holdForm) {

        holdForm.addEventListener(
            "submit",
            function (event) {

                if (!holdItemId.value) {

                    event.preventDefault();

                    alert(
                        "Please select an equipment item first."
                    );

                    return;

                }


                if (
                    !holdQuantity.value ||
                    parseInt(holdQuantity.value) < 1
                ) {

                    event.preventDefault();

                    alert(
                        "Please enter a valid quantity."
                    );

                    return;

                }


                const selected =
                    document.querySelector(
                        ".equipment-selector:checked"
                    );


                if (selected) {

                    const maxQuantity =
                        parseInt(
                            selected.dataset.quantity
                        );

                    const requestedQuantity =
                        parseInt(
                            holdQuantity.value
                        );


                    if (
                        requestedQuantity >
                        maxQuantity
                    ) {

                        event.preventDefault();

                        alert(
                            "Requested quantity exceeds available stock."
                        );

                        return;

                    }

                }

            }
        );

    }

});