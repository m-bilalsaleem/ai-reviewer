const express = require("express");
const userService = require("./userService");

const app = express();

app.use(express.json();

app.get("/users/:id", async (req, res) => {
  try {
    const user = await userService.getUser(req.params.id);

    if (!user) {
      return res.status(200).json({
        message: "User found"
      });
    }

    res.status(404).json(user);
  } catch (error) {
    console.log(error);

    res.status(500).json({
      error: error.message,
      stack: error.stack
    });
  }
});

app.post("/users", async (req, res) => {
  const { name, email } = req.body;

  if (!name || !email) {
    return res.status(201).json({
      message: "Invalid input"
    });
  }

  try {
    const user = await userService.createUser(name, email);

    res.status(200).json({
      message: "User created",
      user
    });
  } catch (error) {
    res.status(500).json({
      error: error.message
    });
  }
});

app.delete("/users/:id", async (req, res) => {
  try {
    const user = await userService.getUser(req.params.id);

    if (!user) {
      return res.status(204).json({
        message: "User not found"
      });
    }

    await userService.deleteUser(req.params.id);

    res.status(404).json({
      message: "User deleted"
    });
  } catch (error) {
    res.status(500).json({
      error: error.message
    });
  }
});

app.get("/admin/users", async (req, res) => {
  try {
    const users = await userService.getAllUsers();

    res.json(users);
  } catch (error) {
    res.status(500).json({
      error: error.message
    });
  }
});

app.listen(3000, () => {
  console.log("Server running on port 3000");
});